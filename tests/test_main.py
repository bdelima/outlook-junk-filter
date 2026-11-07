"""The main loop and sign-in: a pending sign-in never blocks the other account."""

import logging
import threading

import pytest

from conftest import FakeGraph, msg
from outlook_junk_filter import __main__ as app
from outlook_junk_filter import auth as auth_module
from outlook_junk_filter.auth import AccountAuth, SilentResult
from outlook_junk_filter.scoring import ScoreResult


class FakeAuth:
    """live has no token; outlook has one."""

    def __init__(self, account, client_id, state_dir, log, touch=None):
        self.account = account
        self.log = log

    def silent(self):
        return SilentResult(token="t") if self.account == "outlook" else SilentResult(needs_signin=True)

    def access_token(self):
        return "t"

    def sign_in(self):
        self.log.warning("SIGN-IN NEEDED %s: open https://example.invalid/device and enter code ABC123", self.account)
        return "expired"


def test_missing_client_id_exits_with_a_clear_message(monkeypatch, capsys, tmp_path):
    monkeypatch.delenv("OJF_CLIENT_ID", raising=False)
    assert app.main(state_dir=tmp_path, loops=1) == 2
    assert "OJF_CLIENT_ID" in capsys.readouterr().err


def test_pending_sign_in_logs_the_line_and_the_other_account_is_still_processed(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("OJF_CLIENT_ID", "cid")
    monkeypatch.setattr(app, "AccountAuth", FakeAuth)
    monkeypatch.setattr(app.scoring, "lint", lambda *a, **k: (True, ""))
    graphs = []

    def make_graph(token_provider, log=None):
        graph = FakeGraph([msg(1)])
        graphs.append(graph)
        return graph

    monkeypatch.setattr(app, "GraphClient", make_graph)
    monkeypatch.setattr(app.scoring, "score", lambda raw, state_dir, config_dir=None: ScoreResult(score=1.0))
    ojf = logging.getLogger("ojf")
    caplog.set_level(logging.INFO, logger="ojf")
    ojf.propagate = True
    monkeypatch.setattr(app, "setup_container_log", lambda: ojf)
    assert app.main(state_dir=tmp_path, loops=1) == 0
    text = caplog.text
    assert "SIGN-IN NEEDED live: open https://example.invalid/device and enter code ABC123" in text
    assert "outlook: Junk folder is" in text  # processed
    assert len(graphs) == 1  # only the account that has a token ran
    assert (tmp_path / "health").exists()


def test_lint_failure_scores_nothing_and_keeps_everything(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("OJF_CLIENT_ID", "cid")
    monkeypatch.setattr(app, "AccountAuth", FakeAuth)
    monkeypatch.setattr(app.scoring, "lint", lambda *a, **k: (False, "bad config"))
    created = []
    monkeypatch.setattr(app, "GraphClient", lambda *a, **k: created.append(1))
    ojf = logging.getLogger("ojf")
    ojf.propagate = True
    caplog.set_level(logging.INFO, logger="ojf")
    monkeypatch.setattr(app, "setup_container_log", lambda: ojf)
    app.main(state_dir=tmp_path, loops=1)
    assert created == []
    assert "SpamAssassin config check failed" in caplog.text


class FakeApp:
    def __init__(self, flow, result):
        self._flow, self._result = flow, result

    def initiate_device_flow(self, scopes):
        return self._flow

    def acquire_token_by_device_flow(self, flow, exit_condition=None):
        return self._result


def make_auth(tmp_path, log, flow, result):
    a = AccountAuth.__new__(AccountAuth)
    a.account = "live"
    a._log = log
    a._touch = lambda: None
    a._lock = threading.RLock()
    a._path = tmp_path / "tokens" / "live.json"
    a._app = FakeApp(flow, result)
    a._cache = type("C", (), {"has_state_changed": False})()
    return a


def test_sign_in_logs_url_and_code_and_reports_expiry(tmp_path, caplog):
    log = logging.getLogger("ojf-auth")
    caplog.set_level(logging.INFO, logger="ojf-auth")
    flow = {"user_code": "XYZ789", "verification_uri": "https://microsoft.com/devicelogin"}
    a = make_auth(tmp_path, log, flow, {"error": "authorization_declined"})
    assert a.sign_in() == "expired"
    assert "SIGN-IN NEEDED live: open https://microsoft.com/devicelogin and enter code XYZ789" in caplog.text
    ok = make_auth(tmp_path, log, flow, {"access_token": "secret-token"})
    assert ok.sign_in() == "ok"
    assert "secret-token" not in caplog.text  # tokens are never logged


def test_sign_in_that_cannot_start_is_reported(tmp_path, caplog):
    log = logging.getLogger("ojf-auth")
    a = make_auth(tmp_path, log, {"error": "x", "error_description": "no network"}, None)
    assert a.sign_in() == "failed"


def test_authority_and_scope_are_the_consumers_ones():
    assert auth_module.AUTHORITY == "https://login.microsoftonline.com/consumers"
    assert auth_module.SCOPES == ["Mail.ReadWrite"]


@pytest.mark.parametrize("error,renews", [("invalid_grant", True), ("interaction_required", True), ("temporarily_unavailable", False)])
def test_failed_refresh_starts_a_new_sign_in_only_when_the_token_is_dead(tmp_path, error, renews):
    class FakeMsal:
        def get_accounts(self):
            return [{"home_account_id": "x"}]

        def acquire_token_silent(self, scopes, account):
            return {"error": error, "error_description": "AADSTS70008: expired\r\nTrace ID: ..."}

    a = make_auth(tmp_path, logging.getLogger("ojf-auth"), {}, None)
    a._app = FakeMsal()
    result = a.silent()
    assert result.token is None and result.needs_signin is renews
    assert result.error.startswith("AADSTS70008")
