"""
service/map_formatter.py

Converts a crawled application map (from the application_maps table) into
two artefacts the survey LLMs can consume:

  format_map_as_context(map_data)  ->  compact text block for __ELEMENTS__
  identifiers_from_map(map_data)   ->  set of element names for matched check
  format_navigation_flow(map_data) ->  click-by-click path between screens

The crawl agent stores elements in the shape produced by core.tools.get_snapshot:
    {"ref": "el-0", "role": "button", "name": "Create Issue"}

All functions work with that shape and are application-agnostic.
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Hard cap on characters sent to the LLM. The survey prompt shares a context
# window with the requirement text and the reply — exceeding this would push
# requirement content out of the window.
_MAX_MAP_CHARS = int(__import__("os").environ.get("MAP_CONTEXT_MAX_CHARS", "6000"))


def format_map_as_context(map_data: dict) -> str:
    """
    Render a crawled map as a compact, token-efficient text block.

    Each screen is listed with its URL and interactive elements (one line each).
    The output replaces the live-page element snapshot in the survey prompt
    when a pre-crawled map is available.

    Args:
        map_data: dict with key "pages", each entry having:
                    url      (str)
                    title    (str)
                    elements (list[dict])  — each dict has ref, role, name

    Returns:
        Formatted string, or empty string when the map has no pages.
    """
    pages = map_data.get("pages", [])
    if not pages:
        logger.warning("map_formatter: map_data has no pages — returning empty context")
        return ""

    lines = [
        f"=== APPLICATION SCREEN MAP ({len(pages)} screens) ===",
        "Use the screens and elements below to ground test steps in "
        "real, verified UI controls.",
        "",
    ]

    for idx, page in enumerate(pages, 1):
        url = page.get("url", "unknown")
        title = (page.get("title") or url).strip()
        elements = page.get("elements", [])

        lines.append(f"[Screen {idx}] {title}")
        lines.append(f"URL: {url}")

        for el in elements:
            role = (el.get("role") or "element").strip()
            name = (el.get("name") or "").strip()
            ref  = (el.get("ref")  or "").strip()
            iframe_context = (el.get("iframe_context") or "").strip()

            # Compact: "  button: Create Issue  (el-0)"
            # For iframes: "  button: Select File  (ifr-search_modal-el-3)  [in iframe: search_modal]"
            entry = f"  {role}"
            if name:
                entry += f": {name}"
            if ref:
                entry += f"  ({ref})"
            if iframe_context:
                entry += f"  [in iframe: {iframe_context}]"
            lines.append(entry)

        lines.append("")  # blank line between screens

    full = "\n".join(lines)

    # Append the click-by-click flow between screens, when available (maps
    # crawled before this feature, or maps with a single screen, have no
    # usable trace — format_navigation_flow returns "" and output is
    # unchanged from before).
    flow_section = format_navigation_flow(map_data)
    if flow_section:
        full += "\n" + flow_section

    if len(full) > _MAX_MAP_CHARS:
        logger.warning(
            "map_formatter: map context truncated from %d to %d chars",
            len(full), _MAX_MAP_CHARS,
        )
        full = full[:_MAX_MAP_CHARS] + "\n[... map truncated — increase MAP_CONTEXT_MAX_CHARS ...]"

    logger.info(
        "map_formatter: formatted %d screens  total_chars=%d",
        len(pages), len(full),
    )
    return full


def format_navigation_flow(map_data: dict) -> str:
    """
    Render the click-by-click path between crawled screens, so the survey /
    generation LLM can see which control on screen N leads to screen N+1 and
    compose ONE connected end-to-end scenario instead of isolated per-screen
    click tests.

    Built from map_data["trace"] (added by app_map_crawler.crawl_application).
    Maps crawled before this feature was added have no "trace" key — this
    returns "" for them and format_map_as_context's output is unchanged.

    Args:
        map_data: dict with "pages" (as in format_map_as_context) and
                  optionally "trace": a list of step dicts recorded during
                  the crawl, each shaped {"action", "input", "page_index"}.
                  "page_index" is the number of screens recorded *before*
                  that action ran (e.g. page_index=1 means the action ran
                  while looking at the 1st recorded screen).

    Returns:
        Formatted string (starts with a blank line so it reads as a
        separate section), or "" when there is no trace or fewer than two
        screens (nothing to connect).
    """
    pages = map_data.get("pages", [])
    trace = map_data.get("trace", [])
    if not trace or len(pages) < 2:
        return ""

    def _name_for(page_idx: int, ref: str) -> str:
        """Resolve a click/type_text ref to the element's captured name."""
        if not ref or page_idx < 0 or page_idx >= len(pages):
            return ref or "unknown element"
        for el in pages[page_idx].get("elements", []):
            if el.get("ref") == ref:
                return (el.get("name") or el.get("role") or ref).strip()
        return ref

    lines = [
        "",
        "=== NAVIGATION FLOW ===",
        "The sequence below shows exactly which control moved the crawl from "
        "one screen to the next. Use it to write ONE connected end-to-end "
        "scenario across these screens — not isolated single-screen clicks.",
        "",
    ]

    starting_len = len(lines)
    current_page_index: Optional[int] = None
    actions_on_page: list[str] = []

    def _flush(page_index_value: Optional[int]) -> None:
        if not page_index_value or page_index_value < 1:
            return  # actions before the first screen was recorded — nothing to attach them to
        idx = page_index_value - 1
        if idx >= len(pages):
            return
        title = (pages[idx].get("title") or pages[idx].get("url") or f"Screen {idx + 1}").strip()
        if actions_on_page:
            lines.append(f"[Screen {page_index_value}] {title}  ->  " + "; ".join(actions_on_page))
        else:
            lines.append(f"[Screen {page_index_value}] {title}")

    for step in trace:
        page_idx = step.get("page_index")
        if page_idx is None:
            continue  # trace recorded before this feature existed — skip safely

        if current_page_index is None:
            current_page_index = page_idx
        elif page_idx != current_page_index:
            _flush(current_page_index)
            actions_on_page = []
            current_page_index = page_idx

        action = step.get("action")
        args = step.get("input") or {}
        lookup_idx = current_page_index - 1

        if action == "click":
            actions_on_page.append(f'clicked "{_name_for(lookup_idx, args.get("ref", ""))}"')
        elif action == "type_text":
            typed_value = args.get("text", "")
            field_name = _name_for(lookup_idx, args.get("ref", ""))
            if typed_value:
                actions_on_page.append(f'entered "{typed_value}" into "{field_name}"')
            else:
                actions_on_page.append(f'entered text into "{field_name}"')
        elif action == "fill_form_for_exploration":
            actions_on_page.append("filled the form with synthetic data")
        elif action == "navigate_to":
            url = args.get("url", "")
            if url:
                actions_on_page.append(f"navigated to {url}")

    _flush(current_page_index)

    if len(lines) <= starting_len:
        return ""  # no attributable actions found — nothing useful to show

    return "\n".join(lines) + "\n"


def identifiers_from_map(map_data: dict) -> set:
    """
    Collect every element identifier (both ref and name) across all crawled screens.

    Used by fast_survey's _parse_scenarios to validate whether a generated
    test step references a real element on the page (the 'matched' flag).
    Includes elements from both the main document and iframes.

    Args:
        map_data: same shape as format_map_as_context.

    Returns:
        Set of lowercased element identifiers: element refs (el-33, ifr-*-el-*) and names.
    """
    out: set = set()
    for page in map_data.get("pages", []):
        for el in page.get("elements", []):
            # Add element ref (e.g., "el-33" or "ifr-frame_1-el-5") for ID-based matching
            ref = (el.get("ref") or "").strip().lower()
            if ref:
                out.add(ref)
            # Add element name (e.g., "Issue Log Title input") for label-based matching
            name = (el.get("name") or "").strip().lower()
            if name:
                out.add(name)
            # Optionally include iframe context in matching
            # (allows matching "Search Modal Button" or "file_search Button" depending on context)
            iframe_ctx = (el.get("iframe_context") or "").strip().lower()
            if iframe_ctx and name:
                # Add a contextualized identifier: "name (iframe_context)"
                contextualized = f"{name} ({iframe_ctx})"
                out.add(contextualized)
    logger.debug("map_formatter: identifiers extracted  count=%d (includes iframe-contextualized)", len(out))
    return out


def format_map_for_scripts(map_data: dict) -> str:
    """
    Format a crawled map for the SCRIPT_PROMPT's __ELEMENTS__ slot.

    Produces a compact multi-state block — one state section per crawled screen —
    so the script generator sees real element names it can turn into
    aria-label / XPath / placeholder selectors.

    The crawl stores elements as {"ref", "role", "name"} where "name" is the
    first non-empty value of: aria-label, placeholder, accessible name, visible
    text. That is exactly what the SCRIPT_PROMPT's selector preference list
    items 3 (aria-label) and 4 (visible text) need.

    Args:
        map_data: same shape as format_map_as_context.

    Returns:
        Formatted string, or empty string when the map has no pages.
    """
    pages = map_data.get("pages", [])
    if not pages:
        logger.warning("map_formatter: format_map_for_scripts — map has no pages")
        return ""

    lines = []
    for page in pages:
        url = page.get("url", "unknown")
        title = (page.get("title") or url).strip()
        elements = page.get("elements", [])

        # State heading matches the format used by _compact_elements so the
        # script generator recognises the block structure.
        lines.append(f"{title} ({url}):")

        if not elements:
            lines.append("  (no interactive elements recorded)")
        else:
            for el in elements:
                role = (el.get("role") or "element").strip()
                name = (el.get("name") or "").strip()
                iframe_context = (el.get("iframe_context") or "").strip()
                locator = el.get("locator") or {}
                strategy = locator.get("strategy")

                # JDE grid cells (e.g. Item Number / Quantity / Lot-Serial in
                # an editable line-item grid) have NO real DOM attribute of
                # their own — no aria-label, no name, no title. The crawl
                # agent instead records a "jde_grid_header" recipe (column
                # header text + row position — see resolve_jde_grid_cell_locator
                # in service/iframe_helper.py). Printing this the same way as
                # a normal attribute-based field (falling through to the
                # default "aria-label" branch below) was a real bug: it told
                # the model to build a CSS selector like
                # input[name='Item Number'], which matches nothing because
                # that attribute never existed on the real element. Fixed:
                # grid cells get their own, honest line format telling the
                # model to call the injected _resolve_jde_grid_cell() helper
                # instead of guessing a CSS attribute selector.
                if strategy == "jde_grid_header":
                    column = locator.get("column", name)
                    row_index = locator.get("row_index", 0)
                    line = f"  {role}  [JDE GRID CELL] column=\"{column}\" row_index={row_index}"
                    if iframe_context:
                        line += f"  [in iframe: {iframe_context}]"
                    lines.append(line)
                    continue
                # Build attribute hints the model can use for selectors. The
                # crawl agent records WHICH real attribute produced "name" in
                # el["locator"]["attr"] (aria-label, placeholder, value, text,
                # or title) — we must print THAT, not always "aria-label", or
                # the model is told to build a selector for an attribute the
                # element does not actually have (e.g. was told
                # aria-label="Home" when the real DOM attribute is
                # title="Home" — a selector built from that matches nothing).
                if name:
                    attr = locator.get("attr")
                    label = attr if attr in ("title", "aria-label", "placeholder", "text", "value") else "aria-label"
                    line = f"  {role}  {label}=\"{name}\""
                else:
                    line = f"  {role}"
                if iframe_context:
                    line += f"  [in iframe: {iframe_context}]"
                lines.append(line)

        lines.append("")  # blank line between screens

    result = "\n".join(lines)

    if len(result) > _MAX_MAP_CHARS:
        logger.warning(
            "map_formatter: script map context truncated from %d to %d chars",
            len(result), _MAX_MAP_CHARS,
        )
        result = result[:_MAX_MAP_CHARS] + "\n[... map truncated — increase MAP_CONTEXT_MAX_CHARS ...]"

    logger.info(
        "map_formatter: format_map_for_scripts  screens=%d  chars=%d",
        len(pages), len(result),
    )
    return result
