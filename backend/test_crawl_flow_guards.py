"""Exercise the real crawl loop with offline browser and model substitutes."""

import copy
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agents import crawl_agent


class ScriptedProvider:
    def __init__(self, calls):
        self.calls = iter(calls)
        self.results = []

    def build_tool(self, definitions):
        return definitions

    def user_text_content(self, text):
        return text

    def call_model(self, *args):
        action = next(self.calls, ("done", {"passed": False}))
        return SimpleNamespace(action=action, candidates=[SimpleNamespace(content="assistant")])

    def extract_function_call(self, response):
        return response.action

    def function_response_content(self, name, result):
        self.results.append((name, copy.deepcopy(result)))
        return result


def snapshot(count=5):
    return [{"ref": "ok", "role": "button", "name": "Confirm (Ctrl+Alt+C)"}] + [
        {"ref": f"field-{i}", "role": "input", "name": f"Field {i}"}
        for i in range(count - 1)
    ]


def run_script(calls, snapshots, flow=None, click_results=None):
    provider = ScriptedProvider(calls)
    driver = MagicMock()
    driver.current_url = "https://example.test/app/"
    driver.title = "Form"
    driver.execute_script.return_value = []
    with (
        patch.object(crawl_agent, "MAX_SCREENS_NO_BRD", 100),
        patch.object(crawl_agent.tools, "get_snapshot", side_effect=snapshots),
        patch.object(crawl_agent.tools, "click", side_effect=click_results, return_value={"ok": True}) as click,
        patch.object(crawl_agent.tools, "type_text", return_value={"ok": True}),
        patch.object(crawl_agent.tools, "press_key", return_value={"ok": True}),
        patch("service.browser_explorer.wait_for_dynamic_content", return_value={"ok": True}),
        patch("time.sleep"),
    ):
        result = crawl_agent.run_crawl(driver, provider, target_flow=flow)
    return provider, result, click.call_count


def test_smaller_valid_snapshot_is_delivered_without_retries():
    provider, _, _ = run_script(
        [("get_snapshot", {})] * 3, [snapshot(113), snapshot(113), snapshot(58)]
    )
    assert len(provider.results[2][1]) == 58
    assert isinstance(provider.results[2][1], list)


def test_explicit_repeated_clicks_and_waits_advance_without_screen_change():
    flow = "Wait for 5 seconds\nClick Confirm button\nWait for 5 seconds\nClick Confirm button\nWait for 5 seconds"
    provider, _, clicks = run_script([
        ("get_snapshot", {}), ("wait_for_dynamic_content", {"timeout": 5}),
        ("click", {"ref": "ok"}), ("get_snapshot", {}),
        ("wait_for_dynamic_content", {"timeout": 5}), ("click", {"ref": "ok"}),
        ("get_snapshot", {}), ("wait_for_dynamic_content", {"timeout": 5}),
        ("get_snapshot", {}),
    ], [snapshot()] * 4, flow)
    assert clicks == 2
    assert "CURRENT STEP (2 of 5)" in provider.results[1][1]["_flow_reminder"]
    assert "CURRENT STEP (4 of 5)" in provider.results[4][1]["_flow_reminder"]


def test_unrequested_repeat_remains_blocked_after_snapshot():
    _, _, clicks = run_script([
        ("get_snapshot", {}), ("click", {"ref": "ok"}),
        ("get_snapshot", {}), ("click", {"ref": "ok"}),
    ], [snapshot()] * 2)
    assert clicks == 1


def test_failed_click_does_not_advance_step():
    provider, _, clicks = run_script([
        ("get_snapshot", {}), ("click", {"ref": "ok"}),
        ("get_snapshot", {}), ("click", {"ref": "ok"}),
    ], [snapshot(5), snapshot(6)], "Click Confirm button\nWait for 5 seconds",
        [{"error": "not ready"}, {"ok": True}])
    assert clicks == 2
    assert "CURRENT STEP (1 of 2)" in provider.results[2][1]["_flow_reminder"]
    assert "CURRENT STEP (2 of 2)" in provider.results[3][1]["_flow_reminder"]


def test_snapshot_read_error_is_not_recorded_as_page():
    provider, result, _ = run_script(
        [("get_snapshot", {})], [[{"error": "page unavailable"}]]
    )
    assert not result["pages"]
    assert "page unavailable" in str(provider.results[0][1])


def test_field_entry_and_tab_do_not_require_changed_control_list():
    provider, _, _ = run_script([
        ("get_snapshot", {}), ("click", {"ref": "field-0"}),
        ("type_text", {"ref": "field-0", "text": "sample"}),
        ("press_key", {"key": "tab"}), ("get_snapshot", {}),
    ], [snapshot()] * 2,
        "Click Field 0 field\nEnter sample in Field 0 field\nPress Tab to trigger validation\nWait for 5 seconds")
    assert "CURRENT STEP (4 of 4)" in provider.results[-1][1]["_flow_reminder"]


def test_wrong_named_click_does_not_advance_or_allow_repeat():
    provider, _, clicks = run_script([
        ("get_snapshot", {}), ("click", {"ref": "ok"}),
        ("get_snapshot", {}), ("click", {"ref": "ok"}),
    ], [snapshot()] * 2, "Click Cancel button\nClick Confirm button")
    assert clicks == 1
    assert "CURRENT STEP (1 of 2)" in provider.results[-1][1]["_flow_reminder"]


def test_second_requested_click_requires_fresh_snapshot():
    provider, _, clicks = run_script([
        ("get_snapshot", {}), ("click", {"ref": "ok"}),
        ("wait_for_dynamic_content", {"timeout": 5}), ("click", {"ref": "ok"}),
        ("get_snapshot", {}), ("click", {"ref": "ok"}),
        ("get_snapshot", {}), ("click", {"ref": "ok"}),
    ], [snapshot()] * 3, "Click Confirm button\nWait for 5 seconds\nClick Confirm button")
    assert clicks == 2
    assert "error" in provider.results[3][1]
    assert "error" in provider.results[-1][1]


def test_failed_type_does_not_advance_step():
    with patch.object(crawl_agent.tools, "type_text", return_value={"error": "not editable"}):
        provider = ScriptedProvider([
            ("type_text", {"ref": "field", "text": "sample"}),
        ])
        driver = MagicMock()
        driver.current_url = "https://example.test/app/"
        crawl_agent.run_crawl(driver, provider, target_flow="Enter sample\nPress Tab")
    assert "CURRENT STEP (1 of 2)" in provider.results[0][1]["_flow_reminder"]


def test_frame_switch_and_changing_snapshots_do_not_advance_wait():
    with patch.object(crawl_agent.tools, "switch_to_frame", return_value={"ok": True}):
        provider, _, _ = run_script([
            ("get_snapshot", {}), ("switch_to_frame", {"frame_ref": "frame-test"}),
            ("get_snapshot", {}),
        ], [snapshot(5), snapshot(6)], "Wait for 5 seconds\nClick Confirm button")
    assert "CURRENT STEP (1 of 2)" in provider.results[-1][1]["_flow_reminder"]