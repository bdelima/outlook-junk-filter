"""The AI settings are checked when the job starts: a malformed address is an error, and the server is asked once."""

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
import requests

from conftest import make_config
from outlook_junk_filter import __main__ as app
from outlook_junk_filter.ai import AiClient
from outlook_junk_filter.auth import SilentResult


@pytest.mark.parametrize(
    "bad",
    [
        "http://192.168.0.9::8000",  # the typo that cost a debugging session
        "http://host:notaport",
        "http://host:99999",
        "http://:8000",
        "http://host with space:8000",
        "192.168.0.9:8000",
        "ftp://host:8000",
    ],
)
def test_a_malformed_ai_address_is_an_error_and_turns_the_ai_off(bad):
    cfg = make_config(OJF_AI_URL=bad)
    assert cfg.ai_url is None
    assert any("OJF_AI_URL" in p and "valid" in p for p in cfg.problems), bad


@pytest.mark.parametrize("good", ["http://192.168.0.9:8000", "http://ojochal:8000/", "https://llm.example.invalid", "http://[::1]:8000"])
def test_well_formed_ai_addresses_are_accepted(good):
    assert make_config(OJF_AI_URL=good).ai_url


class Handler(BaseHTTPRequestHandler):
    status, body = 200, {"data": [{"id": "some-model"}]}

    def do_GET(self):  # noqa: N802
        payload = json.dumps(self.body).encode()
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        return


@pytest.fixture
def server():
    class Local(Handler):
        pass

    httpd = HTTPServer(("127.0.0.1", 0), Local)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield Local, f"http://127.0.0.1:{httpd.server_address[1]}/v3"
    httpd.shutdown()
    httpd.server_close()


def test_check_reports_a_server_that_serves_the_model(server):
    _, url = server
    assert AiClient(url, "some-model", timeout=5).check() == ("info", "model some-model is available")


def test_check_warns_when_the_model_is_not_listed(server):
    _, url = server
    level, detail = AiClient(url, "other-model", timeout=5).check()
    assert level == "warning" and "other-model" in detail and "some-model" in detail


def test_check_tolerates_a_server_without_a_model_list_or_with_an_odd_one(server):
    handler, url = server
    handler.status = 404
    assert AiClient(url, "m", timeout=5).check()[0] == "info"
    handler.status, handler.body = 200, ["not", "a", "dict"]
    assert AiClient(url, "m", timeout=5).check()[0] == "info"


def test_check_flags_http_errors_and_an_unreachable_server(server):
    handler, url = server
    handler.status = 500
    assert AiClient(url, "m", timeout=5).check()[0] == "error"
    assert AiClient("http://127.0.0.1:9/v3", "m", timeout=2).check()[0] == "error"


class SignedIn:
    def __init__(self, *a, **k):
        pass

    def silent(self):
        return SilentResult(token="t")

    def access_token(self):
        return "t"


def run_startup(monkeypatch, tmp_path, caplog, env, checker):
    monkeypatch.setenv("OJF_CLIENT_ID", "cid")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(app, "Auth", SignedIn)
    monkeypatch.setattr(app.scoring, "lint", lambda *a, **k: (True, ""))
    monkeypatch.setattr(app, "AiClient", lambda *a, **k: checker)
    ojf = logging.getLogger("ojf")
    ojf.propagate = True
    caplog.set_level(logging.INFO, logger="ojf")
    monkeypatch.setattr(app, "setup_container_log", lambda: ojf)
    from conftest import FakeGraph

    monkeypatch.setattr(app, "GraphClient", lambda *a, **k: FakeGraph())
    app.main(state_dir=tmp_path, loops=1)
    return caplog.text


class Checker:
    def __init__(self, result):
        self.result = result
        self.checked = 0

    def check(self):
        self.checked += 1
        return self.result


def test_startup_logs_that_the_ai_server_answers(monkeypatch, tmp_path, caplog):
    checker = Checker(("info", "model some-model is available"))
    text = run_startup(monkeypatch, tmp_path, caplog, {"OJF_AI_URL": "http://192.168.0.9:8000"}, checker)
    assert "AI scoring server http://192.168.0.9:8000/v3: model some-model is available" in text
    assert checker.checked == 1 and "ERROR AI scoring server" not in text


def test_startup_logs_an_error_when_the_ai_server_does_not_answer(monkeypatch, tmp_path, caplog):
    checker = Checker(("error", "ConnectionError: refused"))
    text = run_startup(monkeypatch, tmp_path, caplog, {"OJF_AI_URL": "http://192.168.0.9:8000"}, checker)
    assert "ERROR AI scoring server http://192.168.0.9:8000/v3 is not usable: ConnectionError: refused" in text
    assert "skipped until it answers" in text


def test_startup_does_not_check_when_the_ai_is_off_or_the_address_is_malformed(monkeypatch, tmp_path, caplog):
    checker = Checker(("info", "x"))
    text = run_startup(monkeypatch, tmp_path, caplog, {"OJF_AI_URL": "http://192.168.0.9::8000"}, checker)
    assert checker.checked == 0 and "ERROR OJF_AI_URL=" in text
    assert requests is not None
