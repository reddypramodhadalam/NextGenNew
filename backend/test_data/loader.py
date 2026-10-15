"""
test_data/loader.py

Loads externalised test data for a script or crawl.

SINGLE SOURCE OF TRUTH, per application:

    test_data/applications/app_<application_id>.json

    {
      "application_id": 7,
      "environments": {
        "default": {
          "username": "...",
          "password": {"_encrypted": true, "alg": "fernet", "value": "..."},
          "ItemNumber": "48166329",
          "forms": {
            "Sales Order Entry":      {"ItemNumber": "1001-STAR"},
            "Inventory Adjustment":   {"ItemNumber": "2005-CHEM"}
          }
        }
      }
    }

Resolution for any key (e.g. "ItemNumber"), given a target_flow:
    1. environments.<env>.forms.<target_flow>.<key>   — most specific, wins
    2. environments.<env>.<key>                        — app-level default

MATCHING A target_flow TO A "forms" ENTRY — two supported styles:
    (a) Short destination style: target_flow IS the form name, e.g.
        target_flow = "Sales Order Entry". Matched by exact equality.
    (b) Ordered step-list style: target_flow is a full multi-line manual
        procedure, e.g. "Launch the url\nEnter SSO Username\n...Click on
        Sales Order Entry.\n...Enter Item Number in Item Number field...".
        This is what the New Test Suite wizard sends when a reviewer pastes
        one step per line rather than a short navigation phrase (see
        agents/crawl_agent.py's _is_step_list handling). An exact-equality
        match against a short form name like "Sales Order Entry" will NEVER
        succeed here, even though the procedure clearly targets that form
        — the saved override would be silently ignored, and the crawl
        would fall back to typing synthetic placeholder values instead of
        the real ones a reviewer already saved. This was confirmed live:
        a reviewer saved forms."Sales Order Entry".SoldTo, but a step-list
        target_flow caused SoldTo to resolve to nothing, and the crawl
        agent typed "1000" and failed JDE's address-type validation.

        To support this style too, when no exact match is found, this
        module falls back to a substring match: the LONGEST form name that
        appears anywhere inside target_flow (case-insensitive) is used.
        Longest wins so a more specific name (e.g. "Standard Order SO")
        is preferred over a shorter, more generic one (e.g. "Sales") if
        both happen to appear in the same procedure text.

    (c) Tolerant match (added after a live failure): reviewers rarely type
        the form name in the procedure exactly as it is saved in the Test
        Data screen. A saved form "Inventory Adjustment" never appeared
        verbatim in a step list that said "Click on Inventory Adjust" —
        the JDE menu label is shorter — so styles (a) and (b) both missed,
        the saved Branch/Plant/Quantity/Lot values were silently dropped,
        and the crawl typed synthetic values that JDE rejected. The form
        name is therefore also matched word-by-word, case/punctuation
        insensitive, with each word allowed to be a prefix of the word in
        the procedure (or vice-versa, minimum 4 letters), and the words
        must appear consecutively on ONE line of the procedure. No form
        names are hardcoded anywhere — it works for any form name.

    (d) Field-name match (no dependence on the form's name at all): if the
        form name is not recognisable in the procedure, each form is scored
        by how many of ITS OWN saved field names (e.g. "Branch/Plant",
        "ItemNumber", "Lot/Serial") are mentioned in the procedure, again
        word-tolerant and on a single line. The form with the clearly
        highest score wins. A tie, or a single weak hit, matches nothing —
        guessing wrong would type another form's values, which is worse
        than typing none. Field names come from the saved JSON, never from
        code, so this works for any scenario you add through the UI.

"forms" is popped off before returning, so nothing downstream (get_data(),
a crawl agent, _test_env_for()) ever sees nesting or the word "forms" —
every value that leaves this function is flat.

There is deliberately no second file, no test_data/scenarios/ directory,
and no "link this scenario to a file" step. Everything for one application
lives in this one file, edited through GET/PUT /v1/test-data/app. A form
that needs its own value is just another entry under "forms" — never a
new file, never a new mechanism.

MULTI-ACTOR PROCEDURES — "personas" block:
    A procedure that must run as more than one identity in the SAME
    continuous script/run (e.g. a requester creates a record, an
    approver later approves it, an admin performs a final override) needs
    more than one set of stored credentials for one application. These
    live under an optional "personas" block, alongside "forms":

    {
      "application_id": 7,
      "environments": {
        "default": {
          "username": "...",             <- unchanged: the DEFAULT identity,
          "password": {...},                used for the first login of any
                                             script, the SSO precheck, and
                                             every existing single-user flow
          "personas": {
            "requester": {"username": "...", "password": {...}},
            "approver":  {"username": "...", "password": {...}},
            "admin":     {"username": "...", "password": {...}}
          }
        }
      }
    }

    There is NO fixed list of role names anywhere in this module (or
    anywhere downstream) — "personas" is a plain dict, and any key is a
    valid persona name. A procedure needing a 4th, 5th or 10th role is
    purely a JSON edit here; no code change anywhere is required.

    Each persona's username/password is flattened into two plain keys
    before this function returns: "<persona>_username" and
    "<persona>_password" (e.g. "approver_username", "approver_password"),
    decrypting the password the same way a flat top-level password is
    decrypted. See _flatten_personas() below. These then flow through
    service/auth_gateway.py's existing *generic* TEST_<UPPER_KEY> export
    unchanged — that function has no special-case knowledge of personas
    at all, since every key it sees is already flat by the time it gets
    here.

    "personas" is popped off before returning, exactly like "forms" — it
    never reaches get_data() as a nested key.
"""
import json
import logging
import os
import re
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

HERE = Path(__file__).parent
APP_DIR = HERE / "applications"
LEGACY_APP_DIR = HERE.parents[1] / "test_data" / "applications"
DEFAULT_ENV = "default"


def load_test_data(
    application_id: Optional[int] = None,
    environment: Optional[str] = None,
    target_flow: Optional[str] = None,
) -> dict:
    """
    Return this application's test data, resolved to one flat dict.

    Args:
        application_id: Which application's app_<id>.json to read. None
            returns {} — matches every existing caller's fallback pattern.
        environment: Which environment block to read. Defaults to "default".
        target_flow: The flow/screen this crawl or script belongs to — see
            the module docstring for the two supported styles (short
            destination name, or a full multi-line step-list procedure
            that mentions the form name somewhere in its text). When a
            "forms" entry resolves (exact or substring match), those
            values override the app-level ones on a shared key. No match
            (or target_flow=None) means the flat app-level values are
            used untouched — this keeps every existing app without a
            "forms" block working exactly as before.

    Returns:
        A flat dict of key -> value, secrets decrypted. Never contains a
        "forms" key.
    """
    if application_id is None:
        return {}
    env_key = (environment or "").strip() or DEFAULT_ENV
    doc = _read_app_file(application_id)
    env_data = dict((doc.get("environments") or {}).get(env_key) or {})
    forms = env_data.pop("forms", None)
    personas = env_data.pop("personas", None)
    override = _resolve_form_override(forms, target_flow)
    if override:
        env_data.update(override)
    flat = {k: _decrypt_if_needed(v) for k, v in env_data.items()}
    flat.update(_flatten_personas(personas))
    logger.info(
        "test_data: app=%s env=%s resolved keys=%s",
        application_id, env_key, sorted(str(k) for k in flat.keys()),
    )
    return flat


def _flatten_personas(personas) -> dict:
    """
    Flatten environments.<env>.personas.<name>.{username,password} into
    flat "<name>_username" / "<name>_password" keys.

    See the module docstring ("MULTI-ACTOR PROCEDURES") for the full
    shape. This function has no fixed list of role names — any key under
    "personas" is flattened the same way, so adding a new role to a
    procedure is purely a JSON edit, never a code change here or in any
    downstream caller (service/auth_gateway.py's TEST_* export already
    handles an arbitrary flat key generically).

    Args:
        personas: The environment's "personas" dict (already popped off
            of env_data by the caller), or None/empty if this application
            has no named personas configured at all — single-identity
            applications are completely unaffected and this returns {}.

    Returns:
        Flat dict, e.g. {"approver_username": "...", "approver_password":
        "...", ...}. Never contains a "personas" key, and never contains
        a bare "username"/"password" key (those remain the separate,
        unchanged default-identity keys this function does not touch).
    """
    if not isinstance(personas, dict) or not personas:
        return {}
    out = {}
    for name, creds in personas.items():
        if not isinstance(creds, dict):
            continue
        norm_name = re.sub(r"[^A-Za-z0-9]+", "_", str(name).strip()).strip("_").lower()
        if not norm_name:
            continue
        if "username" in creds:
            out[f"{norm_name}_username"] = creds.get("username")
        if "password" in creds:
            out[f"{norm_name}_password"] = _decrypt_if_needed(creds.get("password"))
    return out


def _tokens(text: str) -> list:
    """Lower-case alphanumeric words of text; punctuation/whitespace dropped."""
    return re.findall(r"[a-z0-9]+", (text or "").lower())


def _word_match(a: str, b: str) -> bool:
    """
    True if two words are the same, or one is a prefix of the other.

    The prefix rule needs both sides to be at least 4 letters so short
    words ("so", "to", "po") can never match something unrelated. This is
    what lets "adjust" (menu label) match "adjustment" (saved form name).
    """
    if a == b:
        return True
    if min(len(a), len(b)) < 4:
        return False
    return a.startswith(b) or b.startswith(a)


def _form_match_score(form_name: str, flow_lines: list) -> Optional[tuple]:
    """
    Score how well form_name appears in the procedure, or None for no match.

    All words of form_name must appear consecutively (word_match-tolerant)
    on a single line. Returns (word_count, char_count, last_line_index) so
    the caller can prefer: more specific names first, then the occurrence
    closest to the end of the procedure.
    """
    want = _tokens(form_name)
    if not want:
        return None
    best_line = None
    for idx, line_tokens in enumerate(flow_lines):
        n = len(want)
        for i in range(0, len(line_tokens) - n + 1):
            if all(_word_match(want[j], line_tokens[i + j]) for j in range(n)):
                best_line = idx
                break
    if best_line is None:
        return None
    return (len(want), len("".join(want)), best_line)


def _key_tokens(key: str) -> list:
    """
    Words of a saved field name. Splits camelCase before lower-casing so
    "ItemNumber" -> ["item", "number"], "Branch/Plant" -> ["branch", "plant"],
    "TEST_UNIT_PRICE" -> ["test", "unit", "price"].
    """
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", str(key or ""))
    return _tokens(spaced)


def _resolve_by_field_names(forms: dict, flow_lines: list) -> Optional[str]:
    """
    Pick the form whose saved field names are mentioned most in the procedure.

    Returns the form name, or None when there is no clear winner: needs a
    strictly higher score than every other form, and either 2+ fields hit or
    at least half of that form's fields hit.
    """
    scores = {}
    for name, value in forms.items():
        if not isinstance(value, dict) or not value:
            continue
        hits = 0
        for key in value.keys():
            want = _key_tokens(key)
            if not want:
                continue
            n = len(want)
            found = any(
                all(_word_match(want[j], line[i + j]) for j in range(n))
                for line in flow_lines
                for i in range(0, len(line) - n + 1)
            )
            if found:
                hits += 1
        scores[name] = (hits, len(value))
    if not scores:
        return None
    ranked = sorted(scores.items(), key=lambda kv: kv[1][0], reverse=True)
    top_name, (top_hits, top_total) = ranked[0]
    if top_hits == 0:
        return None
    if len(ranked) > 1 and ranked[1][1][0] == top_hits:
        return None  # tie -> ambiguous, do not guess
    if top_hits < 2 and top_hits * 2 < top_total:
        return None  # weak single hit
    return top_name


def _resolve_form_override(forms, target_flow: Optional[str]) -> Optional[dict]:
    """
    Find which forms.<name> block applies to this target_flow.

    See the module docstring ("MATCHING A target_flow TO A 'forms' ENTRY")
    for the three matching styles, tried in this order:
      (a) exact name, (b) case-insensitive substring, (c) tolerant
      word-by-word match.

    Args:
        forms: The environment's "forms" dict (already popped off of
            env_data by the caller), or None/empty if the app has no
            per-form overrides configured at all.
        target_flow: See load_test_data's docstring.

    Returns:
        The matching form's dict of overrides, or None if nothing matched
        (including when forms itself is empty/absent).
    """
    if not target_flow or not isinstance(forms, dict) or not forms:
        return None

    # (a) Short destination style — cheapest, most common case.
    exact = forms.get(target_flow)
    if isinstance(exact, dict):
        logger.info("test_data: form override matched exactly: %r", target_flow)
        return exact

    # (b) Form name appears verbatim somewhere in a longer procedure.
    # Longest name wins so "Standard Order SO" beats "Sales".
    flow_lower = target_flow.lower()
    best_name = None
    for name, value in forms.items():
        if not isinstance(value, dict):
            continue
        if name.lower() in flow_lower:
            if best_name is None or len(name) > len(best_name):
                best_name = name
    if best_name:
        logger.info("test_data: form override matched by substring: %r", best_name)
        return forms.get(best_name)

    # (c) Tolerant word-by-word match, per line of the procedure.
    flow_lines = [_tokens(line) for line in target_flow.splitlines()]
    best_name, best_score = None, None
    for name, value in forms.items():
        if not isinstance(value, dict):
            continue
        score = _form_match_score(name, flow_lines)
        if score is not None and (best_score is None or score > best_score):
            best_name, best_score = name, score
    if best_name:
        logger.info("test_data: form override matched by tolerant match: %r", best_name)
        return forms.get(best_name)

    # (d) Field-name match — independent of how the form itself is named.
    best_name = _resolve_by_field_names(forms, flow_lines)
    if best_name:
        logger.info("test_data: form override matched by field names: %r", best_name)
        return forms.get(best_name)

    logger.warning(
        "test_data: no 'forms' entry matched the target flow. Saved forms: %s. "
        "Only application-level values will be used.",
        sorted(str(k) for k in forms.keys()),
    )
    return None


def _read_app_file(application_id: int) -> dict:
    """Find and parse app_<id>.json, checking the legacy path as a fallback."""
    path = APP_DIR / f"app_{application_id}.json"
    if not path.exists():
        legacy = LEGACY_APP_DIR / f"app_{application_id}.json"
        path = legacy if legacy.exists() else None
    if not path:
        return {}
    return _read(path)


def _read(path: Path) -> dict:
    with open(path) as f:
        d = json.load(f)
    d.pop("_comment", None)
    return d


def _fernet() -> Optional[Fernet]:
    key = (os.environ.get("TEST_DATA_ENCRYPTION_KEY") or "").strip()
    if not key:
        return None
    return Fernet(key.encode("utf-8"))


def _decrypt_if_needed(value):
    """Decrypt a {"_encrypted": true, "alg": "fernet", "value": "..."} payload."""
    if not (
        isinstance(value, dict)
        and value.get("_encrypted") is True
        and value.get("alg") == "fernet"
        and "value" in value
    ):
        return value
    fernet = _fernet()
    if fernet is None:
        return value
    try:
        return fernet.decrypt(value["value"].encode("utf-8")).decode("utf-8")
    except InvalidToken:
        return value