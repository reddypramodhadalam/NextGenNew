"""Regression tests for SSO runtime behavior in generated scripts."""

from pathlib import Path


def _read(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def test_sso_preamble_waits_before_deciding_to_skip_login():
    source = _read("service/auth_gateway.py")

    # Guard against the previous race where redirect check happened only once.
    assert "_needs_interactive_sso" in source
    assert "while not _needs_interactive_sso" in source
    assert "driver.find_elements(By.CSS_SELECTOR" in source
    assert "input[name='loginfmt']" in source


def test_sso_preamble_gates_credential_entry_on_interactive_login():
    source = _read("service/auth_gateway.py")

    # Credential entry must be inside the interactive-login branch.
    gate_pos = source.find("if _needs_interactive_sso:")
    user_send_pos = source.find("send_keys(get_data(\"username\"))")
    pass_send_pos = source.find("send_keys(get_data(\"password\"))")

    assert gate_pos != -1
    assert user_send_pos > gate_pos
    assert pass_send_pos > gate_pos


def test_conftest_template_supports_session_cookie_seed():
    template = _read("templates/conftest_template.py")
    assert "SSO_SESSION_COOKIES_JSON" in template
    assert "def _seed_sso_session_cookies" in template
    assert "_seed_sso_session_cookies(chrome)" in template
