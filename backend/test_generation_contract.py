"""Unit tests for service.generation_contract."""

from service.generation_contract import (
    build_generation_contract,
    render_contract_for_prompt,
    validate_generated_script_navigation,
)


def test_build_contract_detects_auth_and_navigation_targets():
    contract = build_generation_contract(
        title="Login and open profile",
        steps=[
            {"step": "Enter username in field", "expected": "value accepted"},
            {"step": "Click settings", "expected": "menu opens"},
        ],
        preconditions=[
            "User is authenticated",
            "User is on the profile page",
        ],
    )
    assert contract["authentication_required"] is True
    assert contract["needs_navigation_context"] is True
    assert contract["expects_navigation_outcome"] is False
    assert "profile" in " ".join(contract["navigation_targets"])


def test_validate_script_navigation_flags_missing_auth_data_reads():
    contract = {
        "authentication_required": True,
        "needs_navigation_context": False,
        "navigation_targets": [],
    }
    code = "def test_x(driver):\n    driver.get(APP_URL)\n"
    issues = validate_generated_script_navigation(contract, code)
    assert issues
    assert "username" in issues[0].lower()


def test_validate_script_navigation_passes_when_contract_is_satisfied():
    contract = {
        "authentication_required": True,
        "needs_navigation_context": True,
        "expects_navigation_outcome": True,
        "navigation_targets": ["profile"],
    }
    code = (
        "def test_x(driver):\n"
        "    driver.get(APP_URL)\n"
        "    u = get_data('username')\n"
        "    p = get_data('password')\n"
        "    menu = driver.find_element(By.ID, 'menu')\n"
        "    menu.click()\n"
        "    original_url = driver.current_url\n"
        "    assert driver.current_url != original_url\n"
        "    # navigate to profile screen\n"
        "    assert u and p\n"
    )
    assert validate_generated_script_navigation(contract, code) == []


def test_validate_script_navigation_flags_missing_navigation_outcome_assertion():
    contract = {
        "authentication_required": False,
        "needs_navigation_context": True,
        "expects_navigation_outcome": True,
        "navigation_targets": ["support"],
    }
    code = (
        "def test_x(driver):\n"
        "    driver.get(APP_URL)\n"
        "    btn = driver.find_element(By.ID, 'support')\n"
        "    btn.click()\n"
        "    assert btn.is_displayed()\n"
    )
    issues = validate_generated_script_navigation(contract, code)
    assert any("navigation outcome" in i.lower() for i in issues)


def test_render_contract_for_prompt_contains_flags_and_targets():
    contract = {
        "title": "Profile validation",
        "authentication_required": True,
        "needs_navigation_context": True,
        "navigation_targets": ["profile", "settings"],
        "required_data_keys": ["username"],
        "step_count": 2,
        "precondition_count": 1,
    }
    rendered = render_contract_for_prompt(contract)
    assert "authentication_required: True" in rendered
    assert "navigation_targets: profile, settings" in rendered
