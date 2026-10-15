"""Generate structured mapped procedures using a fixed, standalone runtime.

The model selects captured target IDs and arranges approved steps. It cannot
invent selectors, helper implementations, input values, or skipped steps.
"""

import ast
import inspect
import json
import logging
import os
import re

from service import grounded_runtime
from service.iframe_helper import _JDE_GRID_HEADER_JS

logger = logging.getLogger(__name__)

# How many times generate_grounded_script() will ask the model to rewrite a
# rejected script before giving up (status=incomplete). Previously fixed at
# 2, which is a tight budget for ANY multi-screen scenario: when the same
# control label (e.g. a JDE "OK (Ctrl+Alt+O)" button, or any Next/Submit/
# Cancel button reused across several screens) produces several different
# catalog target IDs, the model has to disambiguate between them using only
# each candidate's bare "target_N" string -- see _describe_target below,
# which is the real fix for that ambiguity. Attempts are kept configurable
# (rather than just raised) since a higher default still costs additional
# LLM calls per retry; override via GROUNDED_GENERATION_MAX_ATTEMPTS if a
# different budget is needed for a given deployment.
_MAX_GENERATION_ATTEMPTS = int(os.environ.get("GROUNDED_GENERATION_MAX_ATTEMPTS", "4"))

# Screen-context validation (see _screen_context_errors). On by default; set
# GROUNDED_SCREEN_CONTEXT_CHECK=0 to fall back to the previous behaviour.
_SCREEN_CONTEXT_CHECK = (os.environ.get("GROUNDED_SCREEN_CONTEXT_CHECK", "1")
                         .strip().lower() not in {"0", "false", "no", "off"})


def build_catalog(map_data):
    catalog, seen = {}, {}
    recipe_titles = {}
    pages = map_data.get("pages") or []
    frames = {}
    for page in pages:
        for element in page.get("elements") or []:
            ref = element.get("ref") or ""
            if element.get("role") == "iframe" and ref.startswith("frame-"):
                descriptor = {"identifier": ref[6:], "title": element.get("name", "")}
                frames[ref[6:]] = descriptor
    for page_number, page in enumerate(pages, 1):
        for element in page.get("elements") or []:
            recipe = element.get("locator") or {}
            if recipe.get("strategy") not in {"attribute", "ancestor_attribute", "jde_grid_header"}:
                continue
            context = element.get("iframe_context")
            if context and context not in frames:
                # Do not guess a missing frame or use a title as a DOM name.
                continue
            entry = {"name": element.get("name", ""), "role": element.get("role", ""),
                     "locator": recipe, "frames": [frames[context]] if context else None,
                     "candidate_frames": [] if context else list(frames.values())}
            recipe_key = dict(recipe)
            if recipe_key.get("strategy") == "ancestor_attribute":
                recipe_key["strategy"] = "attribute"
            recipe_key = json.dumps(recipe_key, sort_keys=True)
            title = grounded_runtime._space(page.get("title"))
            recipe_titles.setdefault(recipe_key, set()).add(title)
            entry["recipe_key"] = recipe_key
            identity = json.dumps(entry, sort_keys=True)
            if identity in seen:
                catalog[seen[identity]]["screens"].append(page_number)
                continue
            key = "target_" + str(len(catalog) + 1)
            seen[identity] = key
            entry["screens"] = [page_number]
            catalog[key] = entry
    for entry in catalog.values():
        titles = recipe_titles[entry.pop("recipe_key")]
        entry["screen_titles"] = sorted(titles)
        entry["transition_title"] = next(iter(titles)) if len(titles) == 1 and all(titles) else None
    return catalog


_OPERATIONS = {
    "_target", "_click_target", "_enter_text", "_press_tab", "_assert_value",
    "_assert_nonempty", "_optional_click",
    "_assert_screen",
    # Capture-and-reuse: read a runtime-generated value (an order number, a
    # quantity before a change) and reuse or compare it later in the SAME
    # test run. See service/grounded_runtime.py for behaviour and docstrings.
    "_capture_value", "_enter_captured", "_assert_contains_captured", "_assert_value_delta",
    # Multi-actor procedures: switch the live session to a different named
    # identity mid-script (requester -> approver -> admin -> ... any number
    # of roles). See service/grounded_runtime.py's _login_as docstring.
    "_login_as",
}


def _action_text(step):
    # Imported/accepted protocols may retain their original numbered prefix.
    # Normalize only for classification; evidence text remains byte-for-byte.
    return re.sub(r"^\s*\d+[.)]\s*", "", step.get("step", ""))


def _semantic_hint(hint):
    """Discard captured references and role wrappers, never treat them as names."""
    text = grounded_runtime._space(hint)
    text = re.sub(r"\b(?:ifr-[\w-]+-)?(?:el|ref|target)[-_]\d+\b", "", text, flags=re.I)
    text = re.sub(r"\(\s*\)|\[\s*\]", "", text)
    text = re.sub(r"^(?:input|textarea|textbox|button|table|div|span|link|menuitem|ref)\s*:\s*",
                  "", text, flags=re.I)
    return text.strip(" \t:")


def _primary_target(step):
    """Extract the action's field/control, excluding data and screen context."""
    hint = _semantic_hint(step.get("element_hint"))
    text = hint or _action_text(step)
    text = re.sub(r"^\s*(?:enter|type|fill|click|press tab)\b\s*", "", text, flags=re.I)
    # Value-first input instructions: Enter {{KEY}} into the Reference field.
    if (not hint and re.match(r"\s*(?:enter|type|fill)\b", _action_text(step), re.I)
            and re.match(r"(?:\{\{|['\"]|(?:a |the )?(?:value|text|data|number)\b)", text, re.I)):
        destination = re.search(r"\b(?:into|in)\s+(?:the\s+)?(.+)", text, re.I)
        if destination:
            text = destination[1]
    text = re.split(r"\s+in\s+(?:the\s+)?[^.;]*\b(?:screen|page|grid|dialog|workspace|table)\b",
                    text, maxsplit=1, flags=re.I)[0]
    text = re.split(r"\s+(?:on|within|from|with|using|if|when|then)\b|[;\n]", text,
                    maxsplit=1, flags=re.I)[0]
    text = re.sub(r"\{\{[^{}]*\}\}", "", text)
    text = re.sub(r"\s*\(\s*(?:el|ref)[-_]\d+\s*\)", "", text, flags=re.I)
    text = re.sub(r"^(?:in|into|on)\s+", "", text, flags=re.I)
    text = re.sub(r"^the\s+", "", text, flags=re.I)
    text = text.strip(" \t.:'\"`")
    text = re.sub(r"\s+(?:(?:input|textarea|textbox|text box)\s+)?(?:field|input|textbox|text box|cell)$",
                  "", text, flags=re.I)
    return grounded_runtime._space(text).casefold()


def _input_target(entry):
    # Missing role is allowed for legacy synthetic catalogs. Captured menus and
    # tables cannot become typing targets; runtime still checks editability.
    return ("role" not in entry or entry.get("role") in {"input", "textarea", "textbox", "spinbutton"}
            or entry.get("locator", {}).get("strategy") == "jde_grid_header")


def _input_labels(entry):
    labels = [entry.get("name", "")]
    recipe = entry.get("locator") or {}
    # Semantic captured attributes also support legacy synthetic display names.
    if recipe.get("attr") in {"title", "aria-label", "placeholder"}:
        labels.append(recipe.get("value", ""))
    if recipe.get("strategy") == "jde_grid_header":
        labels.append(recipe.get("column", ""))
    return {grounded_runtime._space(label).casefold() for label in labels if label}


def _describe_target(key, catalog):
    """Human-readable summary of one catalog target: "target_28 (name='OK
    (Ctrl+Alt+O)', screen 4)".

    WHY THIS EXISTS: every prompt section and every corrective error message
    in this file previously showed bare IDs like "target_28" — with zero
    information about what that ID actually is or which screen it belongs
    to. That is not a problem when a control's name is unique across the
    whole captured map, but it is a real, generic gap for the extremely
    common enterprise pattern of a control label RECURRING across several
    different screens of the same flow (an "OK" / "Next" / "Submit" /
    "Cancel" button reappearing on each step of a multi-screen wizard, each
    occurrence captured as its own distinct catalog entry because the
    underlying DOM recipe differs per screen). _named_targets() matches
    purely by name text and correctly returns ALL such same-named entries
    as equally valid candidates — there is no bug in treating them as
    tied. The actual gap is downstream: nothing then told the model WHICH
    of those tied, identically-named candidates belongs to the screen
    relevant to the step actually being written, so a plausible-looking
    but wrong choice was easy to make, and a bare-ID rejection message gave
    no new information to correct it on retry — confirmed live: an
    "_target" readiness check was rejected for using a target outside a
    4-candidate whitelist, identically, on two consecutive attempts.

    Applies to every step-list prompt section (NAMED ACTION TARGETS,
    SCREEN PROOF TARGETS, READINESS TARGETS) and to the corresponding
    validate_body() error messages, so the model has the same
    screen-disambiguating information available up front AND in any
    corrective feedback — not scenario-specific: this benefits any flow
    where a control label repeats across screens, which is common in
    multi-step ERP/wizard-style applications generally, not particular to
    one crawled application or test case.

    Args:
        key: A catalog key (e.g. "target_28"), or any other string — an
            unresolvable key (not present in catalog) is returned as-is
            rather than raising, since callers may pass a model-supplied
            value that turned out not to be a real catalog entry at all.
        catalog: The full target catalog from build_catalog().
    Returns:
        A short descriptive string. Never raises.
    """
    entry = catalog.get(key)
    if not isinstance(entry, dict):
        return key
    name = entry.get("name") or "(unnamed)"
    titles = entry.get("screen_titles") or []
    screens = entry.get("screens") or []
    if len(titles) == 1 and titles[0]:
        where = f"screen {titles[0]!r}"
    elif len(screens) == 1:
        where = f"screen {screens[0]}"
    elif screens:
        where = f"screens {sorted(screens)}"
    else:
        where = "screen unknown"
    return f"{key} (name={name!r}, {where})"


def _describe_targets(keys, catalog):
    """_describe_target() applied to a whole collection, sorted for stable
    output (used for prompt sections and error messages alike)."""
    return [_describe_target(key, catalog) for key in sorted(keys)]


def _named_targets(step, catalog):
    """Constrain primary inputs exactly; retain generic click/read matching."""
    action = _action_text(step)
    if re.match(r"\s*(?:enter|type|fill|press tab)\b", action, re.I):
        primary = _primary_target(step)
        return {key for key, entry in catalog.items()
                if primary and _input_target(entry) and primary in _input_labels(entry)}
    if re.match(r"\s*click\b", action, re.I):
        text = _primary_target(step)
    else:
        text = grounded_runtime._space(_semantic_hint(step.get("element_hint")) or action).casefold()
    matches = {}
    for key, entry in catalog.items():
        label = grounded_runtime._space(entry.get("name", "")).casefold()
        if label and re.search(r"(?<!\w)" + re.escape(label) + r"(?!\w)", text):
            matches[key] = len(label)
    longest = max(matches.values(), default=0)
    return {key for key, length in matches.items() if length == longest}


def _approved_final_targets(catalog, approval):
    """Resolve an explicit UI-only approval; never infer one from a failed run."""
    if approval is None:
        return set()
    if not isinstance(approval, dict) or approval.get('scope') != 'ui_only':
        raise ValueError('Final UI verification requires an explicit ui_only scope')
    check = approval.get('check', 'screen')
    if check not in {'screen', 'nonempty'}:
        raise ValueError('Unsupported final UI check')
    required = ('control_name', 'approval_note') + (('screen_title',) if check == 'screen' else ())
    if not all(isinstance(approval.get(key), str) and approval[key].strip() for key in required):
        raise ValueError('Final UI verification requires a screen, control, and approval note')
    targets = {key for key, entry in catalog.items()
               if entry.get('name') == approval['control_name']
               and ((check == 'screen' and entry.get('transition_title') == approval['screen_title'])
                    or (check == 'nonempty' and entry.get('role') in {'input', 'textarea'}))}
    if not targets:
        raise ValueError('Approved UI control must support its check in the captured map')
    return targets


def _proof_targets(number, steps, catalog, *, final_ui_verification=None):
    """Prefer the next approved control, not incidental history/menu labels.

    Screen titles come only from the map. A compound title's final segment may
    name the screen in the approved expected result. No business vocabulary is
    used here. When that name is absent, upcoming data-entry controls provide
    the strongest grounded witness for a conditional confirmation.
    """
    if (final_ui_verification is not None and number == len(steps)
            and final_ui_verification.get('check', 'screen') == 'screen'):
        return _approved_final_targets(catalog, final_ui_verification)
    step = steps[number - 1]
    expected = grounded_runtime._space(step.get("expected")).casefold()
    unique = {key for key, entry in catalog.items() if entry.get("transition_title")}
    titles = set()
    for key in unique:
        title = catalog[key]["transition_title"]
        names = [title, re.split(r"\s+[-–—|]\s+", title)[-1]]
        if any(name and name.casefold() in expected for name in names):
            titles.add(title)
    pool = {key for key in unique if not titles or catalog[key]["transition_title"] in titles}
    conditional = bool(re.search(r"\bif\b|\boptional\b", _action_text(step), re.I))
    if not titles and not conditional:
        return set()
    # Explicit verification hints (including the final endpoint) take priority.
    if re.match(r"\s*(verify|check|assert|wait)\b", _action_text(step), re.I):
        named = _named_targets(step, catalog) & pool
        if named:
            # A shared control can establish readiness, not a screen change.
            return named
        # The step text only matched labels that are NOT on the expected
        # screen (e.g. a menu item "Sales" matched by the words "sales
        # order"). Returning that empty intersection used to leave the step
        # with no possible screen witness at all, so every generation
        # attempt was rejected. Fall through to the grounded witnesses below
        # (next approved action, then stable controls on the expected
        # screen). The expected screen and its heading are still required.
    for following in steps[number:]:
        if not re.match(r"\s*(click|enter|type|fill)\b", _action_text(following), re.I):
            continue
        candidates = _named_targets(following, catalog) & pool
        if candidates:
            return candidates
        # With a known expected screen, do not select a witness from a later
        # screen just because a later action has an easily recognized label.
        if titles:
            break
    if titles:
        # Stable captured attributes are a conservative fallback where the
        # next input is shared across screens. The heading is still required.
        return {key for key in pool if catalog[key].get("locator", {}).get("attr") not in (None, "text")}
    return set()


def _read_targets(number, steps, catalog):
    current = steps[number - 1]
    allowed = _named_targets(current, catalog)
    allowed |= _named_targets({"step": current.get("expected", "")}, catalog)
    for following in steps[number:]:
        if re.match(r"\s*(click|enter|type|fill)\b", _action_text(following), re.I):
            allowed |= _named_targets(following, catalog)
            break
    return allowed | _proof_targets(number, steps, catalog)


def _target_titles(entry):
    """Screen titles a captured target was seen on, or None when unknown."""
    titles = {grounded_runtime._space(t) for t in (entry.get("screen_titles") or [])}
    if not titles or "" in titles:
        return None
    return titles


def _screen_context_errors(blocks, steps, catalog):
    """Reject controls that are not on the screen the procedure is currently on.

    Controls are matched by NAME elsewhere; when a label exists on several
    screens, a step could be written against the copy on a different screen
    and always time out at runtime. Generic rule, captured map data only:
      * _assert_screen(target) / an _optional_click successor fixes the
        current screen to that target's transition_title.
      * A click on a non-input control may change screens: the next different
        control used re-anchors the current screen. Clicking into an input
        never changes screens.
      * Otherwise every control used must be captured on the current screen.
      * Unknown screen (before the first proof) or unknown titles: not checked.
    """
    errors = []
    current = None
    pending = False
    clicked = None
    last_on_screen = None
    for number in sorted(n for n in blocks if isinstance(n, int)):
        if not 1 <= number <= len(steps):
            continue
        for statement in blocks[number].body:
            call = getattr(statement, "value", None)
            if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name):
                continue
            name = call.func.id
            if name not in _OPERATIONS or len(call.args) < 2:
                continue
            if name == "_login_as":
                # A persona switch ends the live session's prior screen
                # state entirely (a fresh Entra login lands back on the
                # app's own post-login landing screen, not wherever the
                # previous identity happened to leave off). Reset to
                # "unknown" -- the same state used before the very first
                # screen proof -- so the next step is never flagged
                # against a screen that belonged to the PREVIOUS identity.
                current = None
                pending, clicked, last_on_screen = False, None, None
                continue
            try:
                target = ast.literal_eval(call.args[1])
            except (ValueError, TypeError):
                continue
            entry = catalog.get(target)
            if not isinstance(entry, dict):
                continue
            if name == "_assert_screen":
                title = entry.get("transition_title")
                if title:
                    current = {grounded_runtime._space(title)}
                    pending, clicked, last_on_screen = False, None, target
                continue
            titles = _target_titles(entry)
            skip = pending and target == clicked
            if current is not None and titles is not None and not skip:
                shared = current & titles
                if pending:
                    current = titles
                    pending, clicked = False, None
                    last_on_screen = target
                elif shared:
                    current = shared
                    last_on_screen = target
                else:
                    suggestions = {key for key in (_read_targets(number, steps, catalog)
                                                   | _proof_targets(number, steps, catalog))
                                   if (_target_titles(catalog[key]) or set()) & current}
                    if last_on_screen:
                        suggestions.add(last_on_screen)
                    hint = (f"; use a control captured on that screen, e.g. one of "
                            f"{_describe_targets(suggestions, catalog)}") if suggestions else (
                        "; use a control captured on that screen (for a wait/readiness step, "
                        "the control just used or the next one acted on)")
                    errors.append(
                        f"Step {number}: {name} uses {_describe_target(target, catalog)}, which was "
                        f"not captured on the current screen {sorted(current)} -- the procedure has "
                        f"not navigated away since the last screen proof{hint}"
                    )
            if name in {"_click_target", "_optional_click"} and not _input_target(entry):
                pending, clicked = True, target
            if name == "_optional_click" and len(call.args) > 2:
                try:
                    successor = catalog.get(ast.literal_eval(call.args[2])) or {}
                except (ValueError, TypeError):
                    successor = {}
                if successor.get("transition_title"):
                    current = {grounded_runtime._space(successor["transition_title"])}
                    pending, clicked = False, None
    return errors


_SSO_TEXT_RE = re.compile(
    r"\bsso\b|\bsign[ -]?in\b|\bstay\s+signed\s+in\b|\busername\b|\bpassword\b|"
    r"\bauthentication\b|\blog[ -]?in\b|\bnext button\b",
    re.I,
)
_SSO_PROMPT_CLICK_RE = re.compile(
    r"^\s*click\b.*\b(?:yes|continue|allow|accept|approve)\b",
    re.I,
)


def _sso_step_numbers(steps, catalog, *, sso_required=False):
    """Return approved steps handled by the canonical SSO preamble.

    Generic, deterministic classification uses approved action + expected
    text. It also includes an affirmative IdP prompt click immediately after
    a recognized sign-in step when that control is absent from the captured
    application map. Authentication context stops at the first grounded app
    control or post-login non-click step.
    """
    if not sso_required:
        return set()
    result = set()
    auth_context = False
    for number, step in enumerate(steps, 1):
        if number == 1:
            continue
        if step.get("login_as"):
            # A persona switch (see _login_as in service/grounded_runtime.py)
            # is its own dedicated, explicitly approved mechanism -- never
            # reclassify it as a generic SSO prompt step just because its
            # wording happens to contain "log in"/"sign in"/etc.
            auth_context = False
            continue
        action = _action_text(step)
        combined = grounded_runtime._space(action + " " + str(step.get("expected") or ""))
        explicit = bool(_SSO_TEXT_RE.search(combined))
        app_targets = _named_targets(step, catalog)
        prompt_click = auth_context and not app_targets and bool(_SSO_PROMPT_CLICK_RE.search(action))
        if explicit or prompt_click:
            result.add(number)
            auth_context = True
            continue
        if app_targets or not re.match(r"\s*click\b", action, re.I):
            auth_context = False
    return result


def _login_as_step_numbers(steps):
    """Return the set of approved step numbers that switch identity.

    Driven ENTIRELY by the approved step's own "login_as" field -- never
    inferred from step wording -- the same design already used for
    capture_as/reuse_captured/compare_to_captured (see validate_body).
    Any non-empty persona name is a valid value; there is no fixed list
    of supported roles.
    """
    return {number for number, step in enumerate(steps, 1) if step.get("login_as")}


_RETAINED_VALUE_RE = re.compile(
    r"\b(?:value|field|input|entry)\b.{0,50}\b(?:remain|remains|retained|retain|"
    r"equal|equals|match|matches|unchanged|preserved|persist(?:s|ed)?|"
    r"still\s+(?:show|display|contain|equal))\b|"
    r"\b(?:remain|remains|retained|retain|equal|equals|match|matches|unchanged|"
    r"preserved|persist(?:s|ed)?)\b.{0,50}\b(?:value|field|input|entry)\b|"
    r"\b(?:field|input)\b.{0,40}\bcontains?\b.{0,40}\b(?:entered|provided|expected)\b|"
    r"\b(?:entered|provided|expected)\s+value\b.{0,40}\b(?:displayed|shown|present)\b",
    re.I,
)


def _requires_retained_value(step):
    """Whether the approved evidence explicitly requires exact value retention.

    A Tab/blur step that merely requests validation, lookup resolution,
    auto-population, stabilization or navigation does not authorize a stronger
    equality assertion against the previously entered value. This rule is
    semantic and application-independent; it never inspects target IDs, field
    names, scenario names or business data.
    """
    expected = grounded_runtime._space(step.get("expected") or "")
    action = _action_text(step)
    return bool(_RETAINED_VALUE_RE.search(expected) or _RETAINED_VALUE_RE.search(action))


def _canonicalize_value_assertions(body, steps):
    """Remove unsupported _assert_value calls from generated step blocks.

    _enter_text already verifies successful entry. A later exact retained-value
    assertion is kept only when the approved action/expected result explicitly
    asks for equality or retention. Invalid Python is left unchanged so the
    existing validator can report it normally.
    """
    try:
        tree = ast.parse(body)
    except SyntaxError:
        return body
    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.FunctionDef):
        return body
    for node in tree.body[0].body:
        if not isinstance(node, ast.With) or len(node.items) != 1:
            continue
        wrapper = node.items[0].context_expr
        if not (isinstance(wrapper, ast.Call) and isinstance(wrapper.func, ast.Name)
                and wrapper.func.id == "step" and len(wrapper.args) >= 2):
            continue
        try:
            number = ast.literal_eval(wrapper.args[1])
        except (ValueError, TypeError):
            continue
        if not isinstance(number, int) or not 1 <= number <= len(steps):
            continue
        if _requires_retained_value(steps[number - 1]):
            continue
        node.body = [statement for statement in node.body if not (
            isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Call)
            and isinstance(statement.value.func, ast.Name)
            and statement.value.func.id == "_assert_value"
        )]
    ast.fix_missing_locations(tree)
    return ast.unparse(tree)


def _canonicalize_sso_blocks(body, sso_steps):
    """Make each classified SSO block contain exactly _sso_step(driver)."""
    if not sso_steps:
        return body
    try:
        tree = ast.parse(body)
    except SyntaxError:
        return body
    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.FunctionDef):
        return body
    for node in tree.body[0].body:
        if not isinstance(node, ast.With) or len(node.items) != 1:
            continue
        call = node.items[0].context_expr
        if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                and call.func.id == "step" and len(call.args) >= 2):
            continue
        try:
            number = ast.literal_eval(call.args[1])
        except (ValueError, TypeError):
            continue
        if number in sso_steps:
            node.body = [ast.Expr(value=ast.Call(
                func=ast.Name(id="_sso_step", ctx=ast.Load()),
                args=[ast.Name(id="driver", ctx=ast.Load())], keywords=[]))]
    ast.fix_missing_locations(tree)
    return ast.unparse(tree)


def validate_body(body, steps, catalog, *, sso_required=False, final_ui_verification=None):
    """Constrain model output to evidence blocks and known runtime operations."""
    errors = []
    sso_steps = _sso_step_numbers(steps, catalog, sso_required=sso_required)
    try:
        approved_ui_targets = _approved_final_targets(catalog, final_ui_verification)
    except ValueError as exc:
        return [str(exc)]
    ui_nonempty = final_ui_verification is not None and final_ui_verification.get('check') == 'nonempty'
    if ui_nonempty and (not steps or not approved_ui_targets <= _named_targets(steps[-1], catalog)):
        return ['UI-only nonempty verification must use the control named in the original final step']
    try:
        tree = ast.parse(body)
    except SyntaxError:
        return ["Invalid Python syntax"]
    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.FunctionDef):
        return ["Return exactly one test function, no imports or helper definitions"]
    function = tree.body[0]
    if function.name != "test_approved_procedure" or [a.arg for a in function.args.args] != ["driver"]:
        errors.append("Use def test_approved_procedure(driver)")
    if function.decorator_list or function.args.defaults:
        errors.append("No decorators or defaults in generated test")
    blocks = {}
    for node in function.body:
        if not isinstance(node, ast.With) or len(node.items) != 1:
            errors.append("Every test statement must be inside one numbered step block")
            continue
        call = node.items[0].context_expr
        if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name) or call.func.id != "step" or len(call.args) != 4:
            errors.append("Use with step(driver, number, action, expected)")
            continue
        try:
            number = ast.literal_eval(call.args[1])
        except (ValueError, TypeError):
            errors.append("Step number must be literal")
            continue
        if number in blocks:
            errors.append("Duplicate step number")
        blocks[number] = node
    if list(blocks) != list(range(1, len(steps) + 1)):
        errors.append("Preserve every approved step exactly once and in order")
    for number, block in blocks.items():
        if not isinstance(number, int) or not 1 <= number <= len(steps):
            continue
        approved = steps[number - 1]
        proof_targets = _proof_targets(number, steps, catalog, final_ui_verification=final_ui_verification)
        wrapper = block.items[0].context_expr
        for position, field in ((2, "step"), (3, "expected")):
            if field in approved:
                try:
                    unchanged = ast.literal_eval(wrapper.args[position]) == approved[field]
                except (ValueError, TypeError):
                    unchanged = False
                if not unchanged:
                    errors.append(f"Step {number}: preserve approved {field} text exactly")
        if any(not isinstance(statement, ast.Expr) or not isinstance(statement.value, ast.Call)
               for statement in block.body):
            errors.append(f"Step {number}: only runtime operation statements are allowed")
        calls = []
        for node in ast.walk(block):
            if isinstance(node, (ast.Try, ast.If, ast.For, ast.While, ast.FunctionDef,
                                 ast.Return, ast.Raise, ast.Lambda, ast.Import, ast.ImportFrom)):
                errors.append(f"Step {number}: no branches, skipped actions, or exception suppression")
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Name):
                # Even driver.find_element/click bypasses the fixed runtime.
                errors.append(f"Step {number}: use runtime operations, not direct methods")
                continue
            name = node.func.id
            if name not in _OPERATIONS | {"step", "_sso_step", "_open_application", "_pause"}:
                errors.append(f"Step {number}: unsupported call {name}")
            calls.append(name)
            if name != "step":
                arguments = node.args[1:] if name != "_pause" else node.args
                if name != "_pause" and (not node.args or not isinstance(node.args[0], ast.Name)
                                         or node.args[0].id != "driver"):
                    errors.append(f"Step {number}: pass driver to runtime operations")
                try:
                    for argument in arguments:
                        ast.literal_eval(argument)
                    for keyword in node.keywords:
                        ast.literal_eval(keyword.value)
                except (ValueError, TypeError):
                    errors.append(f"Step {number}: operation arguments must be literals")
            if name == "_login_as":
                # Not a captured-catalog target at all -- arg[1] is a plain
                # persona name, driven entirely by the approved step's own
                # "login_as" field (same design as capture_as/
                # reuse_captured/compare_to_captured below). Validated on
                # its own; never run through the catalog-target checks.
                try:
                    persona_arg = ast.literal_eval(node.args[1])
                except (IndexError, ValueError, TypeError):
                    persona_arg = None
                expected_persona = approved.get("login_as")
                if expected_persona and str(persona_arg) != str(expected_persona):
                    errors.append(
                        f"Step {number}: _login_as persona {persona_arg!r} differs from "
                        f"approved login_as {expected_persona!r}"
                    )
            elif name in _OPERATIONS:
                try:
                    target = ast.literal_eval(node.args[1])
                    if target not in catalog:
                        errors.append(f"Step {number}: target not in captured catalog")
                    if name in {"_click_target", "_enter_text", "_press_tab", "_optional_click",
                                "_capture_value", "_enter_captured"}:
                        allowed = _named_targets(approved, catalog)
                        named_input = name in {"_enter_text", "_press_tab", "_enter_captured"} and any(
                            _input_labels(entry) for entry in catalog.values())
                        if (allowed or named_input) and target not in allowed:
                            errors.append(
                                f"Step {number}: control differs from the named approved target "
                                f"(you used {_describe_target(target, catalog)}); use one of "
                                f"{_describe_targets(allowed, catalog)}"
                            )
                        if name in {"_enter_text", "_press_tab", "_enter_captured"} and target in catalog and not _input_target(catalog[target]):
                            errors.append(f"Step {number}: text entry requires a captured input control")
                    if name == "_target":
                        allowed = _read_targets(number, steps, catalog)
                        allowed |= proof_targets
                        if any(entry.get("name") for entry in catalog.values()) and target not in allowed:
                            errors.append(
                                f"Step {number}: readiness check used {_describe_target(target, catalog)}, "
                                f"but must use one of the current/next approved controls: "
                                f"{_describe_targets(allowed, catalog)}"
                            )
                    if ui_nonempty and number == len(steps) and name == '_assert_nonempty' and target not in approved_ui_targets:
                        errors.append(f'Step {number}: final nonempty check must use the explicitly approved control')
                    if name == "_optional_click":
                        successor = ast.literal_eval(node.args[2])
                        if successor not in catalog:
                            errors.append(f"Step {number}: missing captured successor")
                        elif not catalog[successor].get("transition_title"):
                            errors.append(f"Step {number}: optional confirmation requires a unique destination control, not a field shared across screens")
                        elif successor not in proof_targets:
                            errors.append(
                                f"Step {number}: successor {_describe_target(successor, catalog)} is not "
                                f"grounded in upcoming approved actions; use one of "
                                f"{_describe_targets(proof_targets, catalog)}"
                            )
                        if not re.search(r"\bif\b|\boptional\b", approved.get("step", ""), re.I):
                            errors.append(
                                f"Step {number}: the approved step text does not say \"if\" or "
                                f"\"optional\", so this action is unconditional here — use "
                                f"_click_target(driver, {target!r}) instead of _optional_click "
                                f"for this step. _optional_click may only be used when the "
                                f"approved step's OWN wording explicitly says the action is "
                                f"conditional (e.g. contains \"if\" or \"optional\")."
                            )
                    if name == "_assert_screen" and not catalog.get(target, {}).get("transition_title"):
                        errors.append(f"Step {number}: screen verification requires a unique destination control")
                    elif name == "_assert_screen" and target not in proof_targets:
                        errors.append(f"Step {number}: use an approved screen witness from {sorted(proof_targets)}, not an incidental label")
                except (IndexError, TypeError, ValueError):
                    errors.append(f"Step {number}: use literal captured target IDs")
            if name == "_assert_value" and not _requires_retained_value(approved):
                errors.append(
                    f"Step {number}: _assert_value is stronger than the approved evidence; "
                    "use it only when the step or expected result explicitly requires the "
                    "entered value to remain, match, equal, or be retained"
                )
            if name == "_enter_text":
                placeholder = re.fullmatch(r"\{\{\s*([^{}]+?)\s*\}\}", str(approved.get("data_input") or ""))
                if placeholder:
                    try:
                        key = ast.literal_eval(node.args[2])
                    except (IndexError, ValueError, TypeError):
                        key = ""
                    if grounded_runtime._normal_key(key) != grounded_runtime._normal_key(placeholder[1]):
                        errors.append(f"Step {number}: data key differs from approved placeholder")
            if name == "_sso_step" and number not in sso_steps:
                errors.append(f"Step {number}: application action cannot be relabelled SSO")
            # Capture-and-reuse: the WHEN is never left to the model's own
            # judgement -- it is decided entirely by whether the approved
            # step itself carries a capture_as / reuse_captured /
            # compare_to_captured field (set during scenario authoring, not
            # by this script-writing call). The model only chooses HOW to
            # satisfy a field that is already there; it cannot invent one
            # unprompted (checked below) or ignore one that is present
            # (checked in the second pass further down, once `calls` is
            # fully known for this block).
            if name == "_capture_value":
                capture_as = approved.get("capture_as")
                if not capture_as:
                    errors.append(f"Step {number}: _capture_value used without an approved capture_as field")
                else:
                    try:
                        variable_arg = ast.literal_eval(node.args[2])
                    except (IndexError, ValueError, TypeError):
                        variable_arg = ""
                    if grounded_runtime._normal_key(variable_arg) != grounded_runtime._normal_key(capture_as):
                        errors.append(f"Step {number}: capture variable name differs from approved capture_as")
            if name == "_enter_captured":
                reuse_captured = approved.get("reuse_captured")
                if not reuse_captured:
                    errors.append(f"Step {number}: _enter_captured used without an approved reuse_captured field")
                else:
                    try:
                        variable_arg = ast.literal_eval(node.args[2])
                    except (IndexError, ValueError, TypeError):
                        variable_arg = ""
                    if grounded_runtime._normal_key(variable_arg) != grounded_runtime._normal_key(reuse_captured):
                        errors.append(f"Step {number}: reused variable name differs from approved reuse_captured")
            if name in {"_assert_contains_captured", "_assert_value_delta"}:
                compare = approved.get("compare_to_captured")
                if not isinstance(compare, dict) or not compare.get("variable"):
                    errors.append(f"Step {number}: {name} used without an approved compare_to_captured field")
                else:
                    try:
                        variable_arg = ast.literal_eval(node.args[2])
                    except (IndexError, ValueError, TypeError):
                        variable_arg = ""
                    if grounded_runtime._normal_key(variable_arg) != grounded_runtime._normal_key(compare["variable"]):
                        errors.append(f"Step {number}: compared variable name differs from approved compare_to_captured")
                    has_delta = compare.get("delta") not in (None, "")
                    if name == "_assert_value_delta" and not has_delta:
                        errors.append(f"Step {number}: _assert_value_delta used but approved compare_to_captured has no delta")
                    if name == "_assert_contains_captured" and has_delta:
                        errors.append(f"Step {number}: approved compare_to_captured specifies a delta; use _assert_value_delta, not _assert_contains_captured")
                    if name == "_assert_value_delta" and has_delta:
                        try:
                            delta_arg = ast.literal_eval(node.args[3])
                        except (IndexError, ValueError, TypeError):
                            delta_arg = None
                        if str(delta_arg) != str(compare["delta"]):
                            errors.append(f"Step {number}: delta differs from approved compare_to_captured")
        text = _action_text(approved)
        if number in sso_steps and calls != ["step", "_sso_step"]:
            errors.append(f"Step {number}: approved SSO step must use ONLY _sso_step(driver)")
        if "_sso_step" in calls and calls != ["step", "_sso_step"]:
            errors.append(f"Step {number}: use ONLY _sso_step(driver); application checks belong to approved post-login steps")
        if sso_required and "_open_application" in calls and calls != ["step", "_open_application"]:
            errors.append(f"Step {number}: use ONLY _open_application(driver); it completes the canonical SSO preamble before returning")
        if approved.get("login_as") and calls != ["step", "_login_as"]:
            errors.append(f"Step {number}: approved login_as requires ONLY _login_as(driver, ...) in this step")
        if "_login_as" in calls and not approved.get("login_as"):
            errors.append(f"Step {number}: _login_as used without an approved login_as field")
        clicks = calls.count("_click_target") + calls.count("_optional_click")
        if clicks and (clicks != 1 or not re.match(r"\s*click\b", text, re.I)):
            errors.append(f"Step {number}: extra business click not in approved action")
        if "_open_application" in calls and (number != 1 or calls.count("_open_application") != 1):
            errors.append(f"Step {number}: unexpected navigation/restart")
        if (re.match(r"\s*(enter|type|fill)\b", text, re.I) and "_sso_step" not in calls
                and not approved.get("login_as")):
            if "_enter_text" not in calls:
                errors.append(f"Step {number}: required text entry is missing")
        if (re.match(r"\s*click\b", text, re.I) and "_sso_step" not in calls
                and not approved.get("login_as")):
            if not {"_click_target", "_optional_click"}.intersection(calls):
                errors.append(f"Step {number}: required click is missing")
        if re.match(r"\s*press tab\b", text, re.I) and "_press_tab" not in calls:
            errors.append(f"Step {number}: required Tab is missing")
        if approved.get("capture_as") and "_capture_value" not in calls:
            errors.append(f"Step {number}: approved capture_as requires a _capture_value call")
        if approved.get("reuse_captured") and "_enter_captured" not in calls:
            errors.append(f"Step {number}: approved reuse_captured requires an _enter_captured call")
        if isinstance(approved.get("compare_to_captured"), dict) and approved["compare_to_captured"].get("variable"):
            if not ({"_assert_contains_captured", "_assert_value_delta"} & set(calls)):
                errors.append(f"Step {number}: approved compare_to_captured requires _assert_contains_captured or _assert_value_delta")
        if len(calls) < 2:
            errors.append(f"Step {number}: empty evidence block")
        if number == len(steps) and not {"_target", "_assert_value", "_assert_nonempty", "_optional_click", "_assert_screen",
                                          "_assert_contains_captured", "_assert_value_delta"}.intersection(calls):
            errors.append(f"Step {number}: final expected outcome has no verification")
        if number == len(steps) and final_ui_verification is not None:
            required_check = '_assert_nonempty' if ui_nonempty else '_assert_screen'
            if required_check not in calls:
                errors.append(f'Step {number}: explicit UI approval requires {required_check}')
            if ui_nonempty and not re.match(r'\s*(enter|type|fill|press tab)\b', text, re.I) and any(
                    call in {'_enter_text', '_press_tab'} for call in calls):
                errors.append(f'Step {number}: final UI observation cannot add input actions')
        if number == len(steps) and re.search(r"\b(saved|persisted|committed)\b", approved.get("expected", ""), re.I):
            # A populated reference can exist before submission. The current
            # runtime has no persistence assertion: only an explicitly expected,
            # captured destination can establish a supported UI completion check.
            # This is deliberately conservative, not a general natural-language
            # proof of business persistence. Never invent a completion endpoint.
            #
            # An exception: comparing a freshly-read value against one CAPTURED
            # earlier in this same run (e.g. "on-hand quantity now equals the
            # quantity captured before adjustment, plus the adjustment amount")
            # is itself a direct, grounded proof that the change took effect --
            # arguably stronger than a screen-heading check, since it confirms
            # the actual business value changed, not just that some screen
            # loaded. Only counts when the step's OWN approved data explicitly
            # requested it (see 'compare_to_captured' validation below); a
            # model cannot introduce this exception on its own by simply
            # calling the operation without the step having asked for it.
            has_value_proof = bool({"_assert_contains_captured", "_assert_value_delta"} & set(calls)) and bool(
                approved.get("compare_to_captured") or approved.get("reuse_captured")
            )
            if not ui_nonempty and not has_value_proof and ("_assert_screen" not in calls or not proof_targets):
                errors.append(f"Step {number}: saved/persisted outcome is unverified; a nonempty field, retained input, or delay is not proof. No approved captured completion-screen assertion is provided; return INCOMPLETE")
        if number == len(steps) and re.search(r"\b(screen|page|dialog)\b", approved.get("expected", ""), re.I):
            final_proofs = [node for node in ast.walk(block) if isinstance(node, ast.Call)
                            and isinstance(node.func, ast.Name) and node.func.id in {"_target", "_assert_screen"}]
            unique_proofs = [node for node in final_proofs if len(node.args) > 1 and isinstance(node.args[1], ast.Constant)
                             and catalog.get(node.args[1].value, {}).get("transition_title")]
            if unique_proofs and "_assert_screen" not in calls:
                errors.append(f"Step {number}: verify the final screen heading with _assert_screen, not just control presence")
    if _SCREEN_CONTEXT_CHECK:
        errors.extend(_screen_context_errors(blocks, steps, catalog))
    return list(dict.fromkeys(errors))


def assemble_script(body, catalog, *, sso_preamble="", final_ui_verification=None, use_shared_driver=False):
    _approved_final_targets(catalog, final_ui_verification)
    runtime = inspect.getsource(grounded_runtime)
    runtime += "\n_TARGETS = " + repr(catalog) + "\n_GRID_INSPECT = " + repr(_JDE_GRID_HEADER_JS) + "\n"
    runtime += '\n# Bounded UI observation only; source protocol text remains unchanged.\n'
    runtime += 'FINAL_UI_VERIFICATION = ' + repr(final_ui_verification) + '\n'
    runtime += 'BUSINESS_PERSISTENCE_VERIFIED = False\n'
    runtime += 'GROUNDED_GENERATION_VERSION = 1\n'
    preamble = "\n".join("    " + line if line.strip() else "" for line in sso_preamble.splitlines())
    runtime += '''
import time
import pytest
from selenium import webdriver
from NextGen2_evidence import step

APP_URL = os.environ.get("APP_URL") or os.environ.get("NETRA_URL")

@pytest.fixture
def driver():
    options = webdriver.ChromeOptions()
    if os.environ.get("HEADED") != "1":
        options.add_argument("--headless=new")
    browser = webdriver.Chrome(options=options)
    browser.set_window_size(1920, 1080)
    try:
        yield browser
    finally:
        browser.quit()

def _open_application(driver):
    if not APP_URL:
        raise RuntimeError("APP_URL is not configured")
    driver.get(APP_URL)
'''
    runtime += (preamble + "\n") if preamble else ""
    runtime += '    _record_event(driver, "application_opened")\n'
    runtime += '''
def _sso_step(driver):
    # The canonical preamble ran during _open_application. Do not replay
    # credentials or a Next click on the authenticated application screen.
    assert driver.current_url, "Authentication preamble did not complete"
    _record_event(driver, "sso_preamble_handled", independent_step_assertion=False)

def _pause(seconds):
    # Preserve an explicitly approved delay; it is never an outcome assertion.
    time.sleep(seconds)

'''
    if use_shared_driver:
        # Production uses the existing conftest fixture (pooling, cookies and
        # cleanup). Diagnostics remain standalone by default.
        tree = ast.parse(runtime)
        fixture = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'driver')
        lines = runtime.splitlines(keepends=True)
        start = min([fixture.lineno] + [node.lineno for node in fixture.decorator_list]) - 1
        runtime = ''.join(lines[:start] + lines[fixture.end_lineno:])
    result = runtime + body + "\n"
    ast.parse(result)
    return result


def generate_grounded_script(*, map_data, steps, scenario, provider, sso_preamble="", preconditions=None,
                             final_ui_verification=None, use_shared_driver=False, diagnostics=None):
    if preconditions:
        # Preserve the established path until structured precondition evidence
        # can be supplied; callers only route here when none are present.
        raise ValueError("Structured grounding does not support extra preconditions")
    catalog = build_catalog(map_data)
    if not catalog:
        if diagnostics is not None:
            diagnostics.append("Captured map has no supported targets; recapture the original procedure.")
        return None
    try:
        _approved_final_targets(catalog, final_ui_verification)
    except ValueError as exc:
        logger.error('Grounded generation incomplete: %s', exc)
        if diagnostics is not None:
            diagnostics.append(str(exc))
        return None
    for number, step in enumerate(steps, 1):
        action = _action_text(step)
        configured_entry = re.match(r"\s*(enter|type|fill)\b", action, re.I) and step.get("data_input")
        sso_entry = bool(sso_preamble) and re.search(r"\bsso\b|username|password", action, re.I)
        if configured_entry and not sso_entry and not _named_targets(step, catalog):
            logger.error("Grounded generation incomplete: required input target missing from captured map at step %d", number)
            if diagnostics is not None:
                diagnostics.append(f"Step {number}: required input target missing; capture the original named field.")
            return None
    sso_steps = _sso_step_numbers(steps, catalog, sso_required=bool(sso_preamble))
    prompt = '''Write exactly def test_approved_procedure(driver): and no other code.
Return Python only. Every approved step must have its own
with step(driver, number, "approved action", "approved expected result") block.
Copy the full step string EXACTLY, including any numbered prefix and refs.
Do not add a docstring or statements outside step blocks.
Keep all numbers, actions, expected results, and conditionality. Never add a
submit, approval, or other business action not requested in the procedure.

USE ONLY THESE PROVIDED OPERATIONS (do not define them):
_open_application(driver): navigate to configured APP_URL, complete SSO when configured.
_sso_step(driver): record an SSO step already handled by the canonical preamble.
_target(driver, "target_id", timeout=60): assert captured control visible in its own frame.
_click_target(driver, "target_id"): resolve exactly one captured control and click once.
_enter_text(driver, "target_id", "DATA_KEY", numeric=False): editable input, keyboard replacement, verify value.
_press_tab(driver, "target_id"): press Tab on that input.
_assert_value(driver, "target_id", "DATA_KEY", numeric=False): verify retained data after validation.
_assert_nonempty(driver, "target_id"): verify an input has a nonempty value.
_assert_screen(driver, "target_id", timeout=60): prove a destination screen using
    a control with non-null transition_title AND its captured visible heading.
_optional_click(driver, "target_id", "successor_target_id", timeout=60):
    successor MUST have non-null transition_title. If destination control AND
    heading are visible, no click; otherwise click once and verify destination.
_pause(seconds): only for an explicitly requested wait; never proof of outcome.
_capture_value(driver, "target_id", "variable_name"): read a control's CURRENT
    value/text and hold it in memory for LATER steps in this same test to
    reuse or compare. ONLY call this when the approved step object itself has
    a "capture_as" field -- use that EXACT string as variable_name. Never call
    this on a step that has no capture_as; never invent a variable_name.
_enter_captured(driver, "target_id", "variable_name"): type a value captured
    earlier by _capture_value into a field. ONLY call this when the approved
    step has a "reuse_captured" field -- use that EXACT string as
    variable_name. It MUST already have been captured by an EARLIER step's
    _capture_value with the same name.
_assert_contains_captured(driver, "target_id", "variable_name"): verify a
    control's current text/value CONTAINS a value captured earlier (e.g. a
    generated reference number now shown in a results row). ONLY call this
    when the approved step has a "compare_to_captured" field with NO "delta"
    key -- use compare_to_captured["variable"] as variable_name.
_assert_value_delta(driver, "target_id", "before_variable", delta): verify a
    control's CURRENT numeric value equals an earlier captured value plus
    delta (a number, or another captured variable name prefixed with "$").
    ONLY call this when the approved step has a "compare_to_captured" field
    WITH a "delta" key -- use compare_to_captured["variable"] as
    before_variable and compare_to_captured["delta"] as delta, copied exactly.
_login_as(driver, "persona_name"): end the current identity's session and
    sign in again as a DIFFERENT named identity (e.g. "approver", "admin",
    or any other role name a scenario defines -- there is no fixed list).
    For a procedure where a later part must be carried out by a different
    person than the one who performed the earlier part (e.g. a requester
    creates something and an approver later approves it), all in ONE
    continuous script. ONLY call this when the approved step object itself
    has a "login_as" field -- use that EXACT string as persona_name; never
    invent one. A step with "login_as" set must contain ONLY this one call
    and nothing else -- no business action, no assertion -- exactly like an
    _sso_step. This is never used for the FIRST login of a script; the
    first login is always the canonical preamble inside
    _open_application()/_sso_step().

Choose IDs from CATALOG by NAME, RECIPE, FRAME, ROLE and SCREEN. IDs are NOT
DOM IDs. Never invent attributes, raw Selenium, code helpers, URLs, input
values, skips, branches or try/except. Grid recipes are supported: do not skip
any grid field. Use DATA_KEY names from placeholders, stripping braces; SSO
credentials belong to the preamble, not application input. After Tab or
validation, call _assert_value ONLY when that approved step or expected result
explicitly says the entered value remains, is retained, equals, matches, is
unchanged, or the field contains the entered/expected value. Validation,
lookup resolution, auto-population, stabilization, or readiness by themselves
do NOT authorize _assert_value. Numeric price fields may normalize decimals.
After a transition, wait for a unique control on the expected screen, not an
old control shared with the previous screen. A nonempty reference or a sleep
does NOT prove the expected screen or a saved record. Do not substitute an
easier assertion for an unprovable expected result: return INCOMPLETE instead.
For a conditional second confirmation use _optional_click, never another
unconditional submit. Select a successor unique to the next screen (grid
fields with non-null transition_title qualify). A shared reference/number
field is NOT a successor proof. Final screen checks MUST use _assert_screen.
When the approved final step has a compare_to_captured field, that comparison
IS the proof of a saved/persisted outcome -- do not also require a screen
heading in that case (see APPROVED STEPS for exact wording and variable
names; never write these steps against a different, unlisted step).
SSO steps must be recorded as preamble-handled, not skipped.
Use ONLY _sso_step(driver) in an SSO step. Do not add application heading
assertions to it. For menu expansion and readiness, use _target on the
current/next approved control, not an arbitrary later menu item.
When SSO is configured the launch step must contain ONLY _open_application(driver).
Do not add a _target check there: the preamble already leaves the login page.
Do not click a final commitment control merely to prove it is visible.
Every control you use must be captured on the screen the procedure is
currently on (check each target's screen_titles). Clicking into an input
field never changes the screen; only a button/link/menu click can. For a
wait or readiness step, check a control on the CURRENT screen (the one just
used, or the next one acted on), never a similarly named control that was
only captured on a different screen.
'''
    prompt += "\nSSO configured: " + str(bool(sso_preamble))
    prompt += "\nSSO STEP NUMBERS (each must contain ONLY _sso_step(driver)): " + json.dumps(sorted(sso_steps))
    login_as_steps = _login_as_step_numbers(steps)
    if login_as_steps:
        prompt += (
            "\nLOGIN_AS STEP NUMBERS (each must contain ONLY "
            "_login_as(driver, \"<its approved login_as value>\")): "
            + json.dumps({str(n): steps[n - 1]["login_as"] for n in sorted(login_as_steps)})
        )
    prompt += "\nSCENARIO:\n" + scenario
    prompt += "\nAPPROVED STEPS:\n" + json.dumps(steps, ensure_ascii=True)
    if final_ui_verification is not None:
        source_derived = final_ui_verification.get('source') == 'original_steps'
        prompt += ('\nSOURCE-DERIVED FINAL UI OBSERVATION:\n' if source_derived
                   else '\nSEPARATELY APPROVED FINAL UI VERIFICATION:\n') + json.dumps(final_ui_verification)
        prompt += ('\nThis is a bounded UI-only observation, not proof of saving or persistence. Keep ALL original '
                   'step and expected text unchanged for traceability. Preserve the LAST step delay. '
                   'For check=nonempty, use _assert_nonempty on the approved control: do NOT require the '
                   'previous screen or editable fields to remain after submission, and do NOT add screen '
                   'assertions or repeat data-entry value checks there. For check=screen (the default), '
                   'use _assert_screen on the approved control. This proves ONLY the approved UI observation, '
                   'NOT persistence, regardless of broader source wording. Do not add any click or navigation '
                   'to reach another endpoint. All earlier steps retain their original verification scope.\n')
    # Candidate scopes are used by the runtime, not guessed by the model.
    prompt_catalog = {key: {k: v for k, v in entry.items() if k != "candidate_frames"}
                      for key, entry in catalog.items()}
    prompt += "\nCATALOG (frames=null means frame provenance missing; runtime checks captured frames):\n"
    prompt += json.dumps(prompt_catalog, ensure_ascii=True, separators=(",", ":"))
    # Each target is shown as "target_N (name='...', screen ...)" rather than
    # a bare ID — see _describe_target's docstring. This matters most when a
    # control label recurs across several screens of the same flow (e.g. an
    # "OK"/"Next"/"Submit" button reused on each step of a multi-screen
    # wizard): several DIFFERENT target IDs then share the exact same name,
    # and a bare ID list gives the model no way to tell them apart before it
    # ever writes a first attempt — not just after a rejection.
    prompt += ("\nEach target below is shown as target_id (name='...', screen ...) for "
               "readability only. In code, pass ONLY the bare target_id string (e.g. "
               "\"target_28\") as the argument — never the name or screen shown in "
               "parentheses; that part is not part of the ID.\n")
    prompt += "\nNAMED ACTION TARGETS (primary actions must use these when provided; assertions may use the expected successor):\n"
    prompt += json.dumps({str(i): _describe_targets(_named_targets(step, catalog), catalog)
                          for i, step in enumerate(steps, 1)})
    prompt += "\nSCREEN PROOF TARGETS: _assert_screen and _optional_click successor MUST use these IDs for that step. They are grounded in approved next actions/expected screens. Empty means screen proof is unavailable, not permission to invent one:\n"
    prompt += json.dumps({str(i): _describe_targets(
                              _proof_targets(i, steps, catalog, final_ui_verification=final_ui_verification), catalog)
                          for i in range(1, len(steps) + 1)})
    prompt += "\nREADINESS TARGETS: _target checks must use these current/next approved controls when listed:\n"
    prompt += json.dumps({str(i): _describe_targets(_read_targets(i, steps, catalog), catalog)
                          for i in range(1, len(steps) + 1)})
    for attempt in range(_MAX_GENERATION_ATTEMPTS):
        response = provider.call_model("Return Python code only.", provider.build_tool([]),
                                       [provider.user_text_content(prompt)])
        body = "\n".join(p.text for p in response.candidates[0].content.parts if getattr(p, "text", ""))
        body = re.sub(r"^```(?:python)?\s*", "", body.strip())
        body = re.sub(r"\s*```$", "", body)
        body = _canonicalize_sso_blocks(body, sso_steps)
        body = _canonicalize_value_assertions(body, steps)
        errors = validate_body(body, steps, catalog, sso_required=bool(sso_preamble),
                               final_ui_verification=final_ui_verification)
        if not errors:
            return assemble_script(body, catalog, sso_preamble=sso_preamble,
                                   final_ui_verification=final_ui_verification, use_shared_driver=use_shared_driver)
        logger.warning("Grounded generation rejected attempt %d: %s", attempt + 1, "; ".join(errors))
        if diagnostics is not None:
            # Unsupported function identifiers originate in model output. Do
            # not echo them (or source/data text) into caller-visible reasons.
            diagnostics.extend(f"Attempt {attempt + 1}: invalid candidate: " +
                               re.sub(r"unsupported call.*", "unsupported runtime call", error)
                               for error in errors)
        prompt += "\nYour previous response was rejected:\n" + "\n".join(errors) + "\nRewrite the entire test correctly."
    return None