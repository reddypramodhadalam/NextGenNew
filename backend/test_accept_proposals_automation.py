"""Contract tests for accept-proposals automation-readiness behavior."""

from __future__ import annotations

from api.routes import run


class _FakeCursor:
    def __init__(self, state: dict):
        self._state = state
        self.rowcount = 0
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql: str, params=None):
        self._state["queries"].append((sql, params))
        norm = " ".join(sql.split()).lower()
        if "select lower(trim(title)) from test_case_proposals" in norm:
            self._rows = []
        else:
            self._rows = []
        self.rowcount = 1

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, state: dict):
        self._state = state

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def cursor(self):
        return _FakeCursor(self._state)

    def commit(self):
        self._state["commits"] += 1


def _install_fakes(monkeypatch):
    """Patch DB/query dependencies so route logic can be tested in isolation."""
    state = {
        "queries": [],
        "commits": 0,
        "created": [],
        "next_id": 100,
    }

    def fake_get_conn():
        return _FakeConn(state)

    def fake_create(**kwargs):
        proposal = {
            "id": state["next_id"],
            "title": kwargs.get("title"),
            "description": kwargs.get("description"),
            "steps": kwargs.get("steps"),
            "origin": kwargs.get("origin"),
            "derivation": kwargs.get("derivation"),
        }
        state["next_id"] += 1
        state["created"].append(proposal)
        return proposal

    monkeypatch.setattr(run, "get_conn", fake_get_conn)
    monkeypatch.setattr(run.proposals_db, "create", fake_create)
    monkeypatch.setattr(run.proposals_db, "update_priority", lambda *_args, **_kwargs: None)
    return state


def test_accept_proposals_downgrades_unsafe_automation(monkeypatch):
    """Field-focused scenario without navigation context is saved but not automated."""
    _install_fakes(monkeypatch)

    body = run.AcceptProposalsRequest(
        application_id=1,
        accepted_by="tester",
        proposals=[
            run.AcceptedProposal(
                title="Validate username field",
                steps=[
                    run.AcceptedStep(step="Enter username in the field", expected="Username is accepted"),
                    run.AcceptedStep(step="Enter password in the field", expected="Password is masked"),
                ],
                preconditions=[],
                should_automate=True,
            )
        ],
    )

    response = run.accept_proposals(body)

    assert response["count"] == 1
    assert response["created"][0]["should_automate"] is False
    assert len(response["automation_warnings"]) == 1
    assert response["created"][0]["tags"]["automation_readiness"]["eligible_for_automation"] is False


def test_accept_proposals_keeps_safe_automation_enabled(monkeypatch):
    """Scenario with explicit screen precondition stays automation-eligible."""
    _install_fakes(monkeypatch)

    body = run.AcceptProposalsRequest(
        application_id=1,
        accepted_by="tester",
        proposals=[
            run.AcceptedProposal(
                title="Login with valid credentials",
                steps=[
                    run.AcceptedStep(step="Enter username", expected="Username is populated"),
                    run.AcceptedStep(step="Click Sign in", expected="Home screen is shown"),
                ],
                preconditions=["User is on the login page"],
                should_automate=True,
            )
        ],
    )

    response = run.accept_proposals(body)

    assert response["count"] == 1
    assert response["created"][0]["should_automate"] is True
    assert response["automation_warnings"] == []
    assert response["created"][0]["tags"]["automation_readiness"]["eligible_for_automation"] is True
