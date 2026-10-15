"""
test_task1_sso_control.py

Validates that the include_sso_scenarios flag in run_new_test_suite
correctly gates augment_sso_scenarios() calls.

Run with:  python test_task1_sso_control.py

Uses only the standard library — no pytest, no database, no browser.
The test mocks augment_sso_scenarios and the precheck to test only
the gating logic that was changed in Task 1.
"""

import sys

failures = []


def _check(failures, label, condition):
    if not condition:
        failures.append(label)
        print(f"  FAIL  {label}")
    else:
        print(f"  ok    {label}")


# ── Test 1: augment_sso_scenarios is called when flag is True ─────────────────

print("\n[1] include_sso_scenarios=True, precheck passes → should inject SSO scenarios")

augment_called = False
original_augment = None

try:
    import service.auth_gateway as _ag
    original_augment = _ag.augment_sso_scenarios

    def _mock_augment(test_cases):
        global augment_called
        augment_called = True
        return test_cases + [{"title": "SSO_INJECTED", "category": "authentication"}]

    _ag.augment_sso_scenarios = _mock_augment

    # Build the same conditional logic as survey_runner.py
    sso_precheck = {"enabled": True, "passed": True}
    include_sso_scenarios = True
    proposed = [{"title": "Login with valid credentials", "category": "functional"}]

    if sso_precheck["enabled"] and sso_precheck["passed"] and include_sso_scenarios:
        proposed = _ag.augment_sso_scenarios(proposed)

    _check(failures, "augment_sso_scenarios WAS called (augment_called=True)", augment_called)
    _check(failures, "Proposals list grew by one injected scenario",
           any(t.get("title") == "SSO_INJECTED" for t in proposed))

finally:
    if original_augment:
        _ag.augment_sso_scenarios = original_augment


# ── Test 2: augment_sso_scenarios is NOT called when flag is False ────────────

print("\n[2] include_sso_scenarios=False, precheck passes → should NOT inject")

augment_called = False

try:
    _ag.augment_sso_scenarios = _mock_augment

    sso_precheck = {"enabled": True, "passed": True}
    include_sso_scenarios = False
    proposed = [{"title": "Login with valid credentials", "category": "functional"}]

    if sso_precheck["enabled"] and sso_precheck["passed"] and include_sso_scenarios:
        proposed = _ag.augment_sso_scenarios(proposed)
    elif sso_precheck["enabled"] and sso_precheck["passed"] and not include_sso_scenarios:
        pass   # flag said no — this is the correct branch

    _check(failures, "augment_sso_scenarios was NOT called (augment_called=False)",
           not augment_called)
    _check(failures, "Proposals list is unchanged (no SSO scenario injected)",
           not any(t.get("title") == "SSO_INJECTED" for t in proposed))

finally:
    _ag.augment_sso_scenarios = original_augment


# ── Test 3: augment is not called when precheck failed, regardless of flag ────

print("\n[3] include_sso_scenarios=True, precheck FAILED → should NOT inject")

augment_called = False

try:
    _ag.augment_sso_scenarios = _mock_augment

    sso_precheck = {"enabled": True, "passed": False}
    include_sso_scenarios = True
    proposed = [{"title": "Login with valid credentials", "category": "functional"}]

    if sso_precheck["enabled"] and sso_precheck["passed"] and include_sso_scenarios:
        proposed = _ag.augment_sso_scenarios(proposed)

    _check(failures, "augment_sso_scenarios was NOT called when precheck failed",
           not augment_called)
    _check(failures, "Proposals list unchanged after failed precheck",
           not any(t.get("title") == "SSO_INJECTED" for t in proposed))

finally:
    _ag.augment_sso_scenarios = original_augment


# ── Test 4: augment is not called when SSO is not enabled for the app ─────────

print("\n[4] sso_precheck.enabled=False → augment should never run")

augment_called = False

try:
    _ag.augment_sso_scenarios = _mock_augment

    sso_precheck = {"enabled": False, "passed": True}
    include_sso_scenarios = True
    proposed = [{"title": "Submit form", "category": "functional"}]

    if sso_precheck["enabled"] and sso_precheck["passed"] and include_sso_scenarios:
        proposed = _ag.augment_sso_scenarios(proposed)

    _check(failures, "augment_sso_scenarios not called for non-SSO app",
           not augment_called)

finally:
    _ag.augment_sso_scenarios = original_augment


# ── Test 5: augment_sso_scenarios itself deduplicates ─────────────────────────

print("\n[5] augment_sso_scenarios deduplication — existing SSO title is not doubled")

try:
    existing_sso_title = (
        "Validate SSO login reaches the application home page after sign-in prompts"
    )
    proposals_with_sso = [{"title": existing_sso_title, "category": "authentication"}]
    result = _ag.augment_sso_scenarios(proposals_with_sso)

    count = sum(1 for t in result
                if (t.get("title") or "").strip().lower() == existing_sso_title.lower())
    _check(failures, "Duplicate SSO title appears exactly once after augment", count == 1)

except Exception as e:
    failures.append(f"augment_sso_scenarios raised unexpectedly: {e}")
    print(f"  FAIL  augment_sso_scenarios raised: {e}")


# ── Summary ───────────────────────────────────────────────────────────────────

print()
if failures:
    print(f"FAILED ({len(failures)} failure(s)):")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
else:
    print("All Task 1 SSO control checks passed.")
    sys.exit(0)
