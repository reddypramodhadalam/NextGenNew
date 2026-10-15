"""Generic map coverage and bounded observations from the original procedure."""

import ast
import hashlib
import json
import re
from datetime import datetime, timezone


def steps_fingerprint(steps):
    return hashlib.sha256(json.dumps(steps or [], sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _supports_action_grammar(steps, preconditions=None):
    """Classify the procedure independently of whether a map covers it."""
    if not steps or preconditions:
        return False
    from service.grounded_generation import _action_text
    for index, step in enumerate(steps):
        if (not isinstance(step, dict) or not isinstance(step.get('expected'), str)
                or not isinstance(step.get('step'), str)):
            return False
        text = _action_text(step)
        if index == 0 and re.match(r'(launch|open|navigate|visit|go to)\b', text, re.I):
            continue
        if not re.match(r'(click|enter|type|fill|press tab|wait|verify|check|assert)\b', text, re.I):
            return False
        if re.match(r'(enter|type|fill)\b', text, re.I) and not re.search(r'\bsso\b|username|password', text, re.I):
            if not re.fullmatch(r'\{\{\s*[^{}]+\s*\}\}', str(step.get('data_input') or '')):
                return False
    return True


def supports_grounded_generation(steps, map_data, preconditions=None):
    """Keep unsupported procedures on the established generation path."""
    from service.grounded_generation import build_catalog
    return bool(_supports_action_grammar(steps, preconditions) and map_data and build_catalog(map_data))


def select_generation_map(steps, maps, preconditions=None, sso_required=False):
    """Select the newest fully covered map; mapped rejection never means legacy.

    The caller must supply complete maps for ONE application (list_complete).
    None is reserved for unsupported grammar, not missing map coverage.
    """
    if not _supports_action_grammar(steps, preconditions):
        return None
    from service.grounded_generation import build_catalog, _action_text, _named_targets, _proof_targets

    required, conditional = [], []
    for number, step in enumerate(steps, 1):
        text = _action_text(step)
        if sso_required and re.search(r'\bsso\b|sign.?in|stay\s+signed\s+in|username|password|\bnext button\b|\byes button\b', text, re.I):
            continue
        if re.match(r'(enter|type|fill|press tab)\b', text, re.I):
            required.append((number, 'input'))
        elif re.match(r'click\b', text, re.I):
            required.append((number, 'click'))
            if re.search(r'\bif\b|\boptional\b', text, re.I):
                conditional.append(number)

    def coverage(map_data):
        catalog = build_catalog(map_data or {})
        errors = []
        if not catalog:
            errors.append('Map has no supported captured controls.')
        for number, kind in required:
            if not _named_targets(steps[number - 1], catalog):
                errors.append(f'Step {number}: missing {kind} target; capture the original named control.')
        for number in conditional:
            # A title or incidental menu alone is not an original-action witness.
            original_targets = _named_targets({'step': steps[number - 1]['expected']}, catalog)
            for following in steps[number:]:
                if re.match(r'(click|enter|type|fill|verify|check|assert)\b', _action_text(following), re.I):
                    original_targets |= _named_targets(following, catalog)
            if not (_proof_targets(number, steps, catalog) & original_targets):
                errors.append(f'Step {number}: missing conditional transition witness; capture a unique destination control from the original procedure.')
        return errors

    def newest(row):
        value = row.get('crawled_at')
        try:
            stamp = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace('Z', '+00:00'))
            stamp = stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp.astimezone(timezone.utc)
        except (ValueError, TypeError):
            stamp = datetime.min.replace(tzinfo=timezone.utc)
        return stamp, int(row.get('id') or 0)

    candidates = sorted((row for row in maps or [] if row.get('status', 'complete') == 'complete'),
                        key=newest, reverse=True)
    rejected = []
    for row in candidates:
        errors = coverage(row.get('map_data'))
        if not errors:
            return {**row, 'selection_summary': {
                'strategy': 'latest_complete_coverage',
                'required_action_steps': [number for number, _ in required],
                'conditional_transition_steps': conditional,
                'rejected_newer_maps': len(rejected),
            }}
        rejected.append(errors)
    reasons = list(dict.fromkeys(error for errors in rejected for error in errors)) if rejected else coverage(None)
    raise ValueError('No suitable complete map covers the original procedure. Recapture its controls and retry. '
                     + ' '.join(reasons))


def infer_final_ui_observation(steps, map_data):
    """Reuse ONLY an earlier explicit nonempty field check at the final hint.

    This reports an on-screen value after the original actions. It neither
    adds actions nor establishes saving, persistence, or a business verdict.
    """
    if not steps or len(steps) < 2:
        return None
    from service.grounded_generation import build_catalog, _action_text, _primary_target, _input_target
    final = steps[-1]
    if not final.get('element_hint'):
        return None
    name = _primary_target({'step': '', 'element_hint': final['element_hint']})
    catalog = build_catalog(map_data or {})
    names = {entry.get('name') for entry in catalog.values()
             if _input_target(entry) and str(entry.get('name', '')).strip().casefold() == name}
    if len(names) != 1:
        return None
    control_name = next(iter(names))
    field = r'(?<!\w)' + re.escape(control_name) + r'(?!\w)'
    if not re.search(field, final.get('expected', ''), re.I):
        return None
    nonempty = r'(?:non[- ]?empty|not\s+empty|has\s+a\s+value|system[- ]generated)'
    # Restrict the nonempty predicate to this field, not a different field in
    # the same step. An element hint alone is not an original verification.
    predicate = (field + r"\s*(?:field\s*)?(?:(?:is|should be|must be|was)\s+)?"
                 + r'(?:automatically\s+)?' + nonempty)
    # Also support adjective-first source wording, e.g. "a non-empty,
    # system-generated Receipt Reference". Do not cross a conjunction into
    # a different field's predicate.
    adjective_predicate = (r'(?:non[- ]?empty|system[- ]generated)'
                           r'(?:,\s*system[- ]generated)?\s+'
                           r'(?:(?!and\b|or\b|but\b|then\b|with\b|is\b)\w+[\s-]+){0,2}' + field)
    for earlier in steps[:-1]:
        action = _action_text(earlier)
        if not re.match(r'\s*(verify|check|assert)\b', action, re.I):
            continue
        if not re.search(field, action, re.I):
            continue
        if any(re.search(pattern, text, re.I)
               for pattern in (predicate, adjective_predicate)
               for text in (action, earlier.get('expected', ''))):
            return {'scope': 'ui_only', 'check': 'nonempty', 'control_name': control_name,
                    'approval_note': 'Source-derived observation from original field verification only; not proof of saving or persistence.',
                    'source': 'original_steps'}
    return None


def is_grounded_script(code):
    """Recognize our generated module structurally, not by a prompt substring."""
    try:
        return any(isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
                   and node.value.value == 1
                   and any(isinstance(target, ast.Name) and target.id == 'GROUNDED_GENERATION_VERSION'
                           for target in node.targets) for node in ast.parse(code).body)
    except (SyntaxError, TypeError):
        return False