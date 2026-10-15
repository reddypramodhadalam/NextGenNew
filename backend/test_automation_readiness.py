"""Unit tests for service.automation_readiness."""

from service.automation_readiness import evaluate_automation_readiness


def test_blocks_without_steps_when_automation_requested():
    result = evaluate_automation_readiness(
        title="No steps",
        steps=None,
        preconditions=[],
        requested=True,
    )
    assert result["eligible_for_automation"] is False
    assert any("No structured steps" in r for r in result["blocked_reasons"])


def test_blocks_field_focused_case_without_navigation_context():
    steps = [
        {"step": "Enter username in the field", "expected": "Username is accepted"},
        {"step": "Enter password in the field", "expected": "Password is masked"},
    ]
    result = evaluate_automation_readiness(
        title="Field validation only",
        steps=steps,
        preconditions=[],
        requested=True,
    )
    assert result["eligible_for_automation"] is False
    assert any("navigation or starting-screen context" in r for r in result["blocked_reasons"])


def test_allows_field_focused_case_with_navigation_precondition():
    steps = [
        {"step": "Enter username in the field", "expected": "Username is accepted"},
        {"step": "Click Sign in", "expected": "Home screen is displayed"},
    ]
    preconditions = ["User is on the login page"]
    result = evaluate_automation_readiness(
        title="Login flow",
        steps=steps,
        preconditions=preconditions,
        requested=True,
    )
    assert result["eligible_for_automation"] is True
    assert result["blocked_reasons"] == []


def test_records_warning_for_missing_expected_results():
    steps = [
        {"step": "Click Settings", "expected": ""},
        {"step": "Click Profile", "expected": "Profile screen opens"},
    ]
    preconditions = ["User is authenticated", "User is on the home screen"]
    result = evaluate_automation_readiness(
        title="Settings navigation",
        steps=steps,
        preconditions=preconditions,
        requested=True,
    )
    assert result["eligible_for_automation"] is True
    assert result["warnings"]
    assert "no expected result" in result["warnings"][0].lower()
