"""The review page over real HTTP: escaping, headers, CSV, the confirm-and-act flow."""

import http.client
import json
import re
import subprocess
import threading
from urllib.parse import urlencode

import pytest

from outlook_junk_filter.graph import GraphError
from outlook_junk_filter.restored import RestoredList
from outlook_junk_filter.ui.actions import Actions
from outlook_junk_filter.ui.server import Context, make_server
from test_ui_logreader import rec, write

HOSTILE = '<script>alert(1)</script>"><img src=x onerror=alert(2)>'


class FakeGraph:
    def __init__(self):
        self.raw_calls, self.moves, self.gone = [], [], False

    def raw(self, message_id):
        self.raw_calls.append(message_id)
        if self.gone:
            raise GraphError(404, "gone")
        return b"Subject: x\r\n\r\nbody"

    def move(self, message_id, destination):
        self.moves.append((message_id, destination))
        if self.gone:
            raise GraphError(404, "gone")
        return "NEWID"


@pytest.fixture
def web(tmp_path):
    state, config = tmp_path / "state", tmp_path / "config"
    config.mkdir()
    log_dir = state / "logs"
    write(log_dir, "decisions.jsonl", [
        rec(1, subject=HOSTILE, from_addr="evil@phish.example", decision="quarantined", folder="Quarantined",
            current_message_id="NEW1", ts="2026-11-05T10:00:00Z"),
        rec(2, subject="=HYPERLINK(\"http://x\")", ts="2026-11-05T10:00:01Z", score=-3.0, decision="deleted",
            folder="deleteditems", current_message_id="NEW2"),
        rec(3, decision="permanently-deleted", folder="", current_message_id="", ts="2026-11-05T10:00:02Z"),
        rec(4, ts="2026-11-05T10:00:03Z"),
    ])
    graph, learn_calls = FakeGraph(), []

    def fake_run(cmd, **kw):
        learn_calls.append((cmd, kw["input"]))
        return subprocess.CompletedProcess(cmd, 0, b"Learned tokens from 1 message(s)", b"")

    restored = RestoredList(state)
    actions = Actions(lambda account: graph, restored, state, config, run=fake_run)
    ctx = Context(log_dir, actions)
    server = make_server(0, ctx, host="127.0.0.1")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield type("W", (), {
        "port": server.server_address[1], "ctx": ctx, "graph": graph, "learn": learn_calls, "state": state,
        "config": config, "restored": restored,
    })
    server.shutdown()
    server.server_close()


def request(web, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", web.port, timeout=10)
    conn.request(method, path, body=body, headers=headers or {})
    resp = conn.getresponse()
    data = resp.read().decode("utf-8")
    result = (resp.status, dict(resp.getheaders()), data)
    conn.close()
    return result


def detail_query(account, message_id, ts):
    return urlencode({"account": account, "id": message_id, "ts": ts})


def test_decisions_page_escapes_a_hostile_subject(web):
    status, headers, body = request(web, "GET", "/")
    assert status == 200
    assert "<script" not in body and "<img" not in body  # no markup from the subject survives
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body
    detail = request(web, "GET", "/message?" + detail_query("live", "M1", "2026-11-05T10:00:00Z"))[2]
    assert "<script" not in detail and "<img" not in detail
    assert "&lt;img src=x onerror=alert(2)&gt;" in detail.replace("&quot;", '"')


def test_security_headers_and_no_external_requests(web):
    for path in ("/", "/summary", "/whatif", "/static/style.css"):
        status, headers, body = request(web, "GET", path)
        assert status == 200
        csp = headers["Content-Security-Policy"]
        assert "script-src 'self'" in csp and "style-src 'self'" in csp and "default-src 'none'" in csp
        assert headers["X-Content-Type-Options"] == "nosniff"
    html = request(web, "GET", "/")[2]
    assert not re.search(r"(src|href)=\"(https?:)?//", html)
    assert "<script" not in html


def test_message_detail_lists_every_field_and_the_rules(web):
    html = request(web, "GET", "/message?" + detail_query("live", "M1", "2026-11-05T10:00:00Z"))[2]
    for name in ("message_id", "internet_message_id", "current_message_id", "folder", "first_untrusted_ip", "action_error"):
        assert f"<dt>{name}</dt>" in html
    assert "<li>A</li>" in html


def test_unknown_message_and_page_are_404(web):
    assert request(web, "GET", "/message?" + detail_query("live", "NOPE", "x"))[0] == 404
    assert request(web, "GET", "/nothing")[0] == 404


def test_csv_export_neutralizes_formulas_and_follows_the_filters(web):
    status, headers, body = request(web, "GET", "/export.csv?decision=deleted")
    lines = body.strip().splitlines()
    assert lines[0] == "time,account,sender,subject,score,decision,reason"
    assert len(lines) == 2
    assert "'=HYPERLINK" in lines[1] and "'-3.0" in lines[1]
    assert headers["Content-Disposition"].startswith("attachment")


def test_what_if_page(web):
    html = request(web, "GET", "/whatif?quarantine=2&delete=9")[2]
    assert "Would be quarantined" in html
    assert "must be lower" in request(web, "GET", "/whatif?quarantine=9&delete=2")[2]
    assert "must be numbers" in request(web, "GET", "/whatif?quarantine=abc")[2]


def confirm_and_act(web, action, message_id="M1", ts="2026-11-05T10:00:00Z", **headers):
    account = "live" if int(message_id[1:]) % 2 else "outlook"  # as rec() assigns it
    query = urlencode({"account": account, "id": message_id, "ts": ts, "action": action})
    status, _, page = request(web, "GET", "/confirm?" + query)
    assert status == 200 and "Confirm" in page
    token = re.search(r'name="token" value="([^"]+)"', page).group(1)
    form = urlencode({"account": account, "id": message_id, "ts": ts, "action": action, "token": token})
    return request(web, "POST", "/act", form, {"Content-Type": "application/x-www-form-urlencoded", **headers})


def actions_log(web):
    path = web.state / "logs" / "actions.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def test_teach_spam_refetches_the_message_and_calls_sa_learn_once(web):
    status, _, body = confirm_and_act(web, "teach-spam")
    assert status == 200 and "Taught Bayes: spam" in body
    assert web.graph.raw_calls == ["NEW1"]  # the page re-fetches by the current id
    (cmd, raw), = web.learn
    assert cmd[0] == "sa-learn" and "--spam" in cmd and "--ham" not in cmd and raw.startswith(b"Subject:")
    status, _, body = confirm_and_act(web, "teach-ham")
    assert "--ham" in web.learn[1][0]
    log = actions_log(web)
    assert [r["action"] for r in log] == ["teach-spam", "teach-not-spam"]
    assert all(set(r) == {"ts", "action", "account", "message_id", "target"} for r in log)  # never a body


def test_teaching_a_message_gone_from_outlook_is_refused_with_a_clear_message(web):
    web.graph.gone = True
    status, _, body = confirm_and_act(web, "teach-spam")
    assert status == 400 and "no longer in Outlook" in body
    assert web.learn == [] and actions_log(web) == []


def test_permanently_deleted_rows_offer_no_action(web):
    html = request(web, "GET", "/message?" + detail_query("live", "M3", "2026-11-05T10:00:02Z"))[2]
    assert "/confirm" not in html and "view-only" in html or "gone from Outlook" in html
    status, _, body = confirm_and_act(web, "teach-spam", "M3", "2026-11-05T10:00:02Z")
    assert status == 400 and web.learn == []
    status, _, body = confirm_and_act(web, "restore-junk", "M3", "2026-11-05T10:00:02Z")
    assert status == 400 and web.graph.moves == []


def test_allowlist_writes_the_expected_line_and_is_idempotent(web):
    status, _, body = confirm_and_act(web, "allow-domain")
    assert status == 200
    confirm_and_act(web, "allow-domain")
    confirm_and_act(web, "allow-address")
    text = (web.config / "ojf-allowlist.cf").read_text(encoding="utf-8")
    assert text.count("whitelist_auth *@phish.example") == 1
    assert "whitelist_auth evil@phish.example" in text
    assert [r["action"] for r in actions_log(web)].count("allowlist") == 3
    assert not (web.config / "local.cf").exists()  # local.cf stays hand-edited


def test_allowlist_refuses_a_sender_that_is_not_a_plain_address(web):
    write(web.ctx.log_dir, "decisions.jsonl", [rec(9, from_addr="x@y.example\nwhitelist_auth *", ts="2026-11-06T00:00:00Z")])
    status, _, body = confirm_and_act(web, "allow-address", "M9", "2026-11-06T00:00:00Z")
    assert status == 400 and not (web.config / "ojf-allowlist.cf").exists()


def test_restore_moves_the_message_and_records_it_so_the_job_keeps_it(web):
    status, _, body = confirm_and_act(web, "restore-junk")
    assert status == 200
    assert web.graph.moves == [("NEW1", "junkemail")]
    assert web.restored.contains("<1@x.invalid>")
    status, _, _ = confirm_and_act(web, "restore-inbox", "M2", "2026-11-05T10:00:01Z")
    assert web.graph.moves[-1] == ("NEW2", "inbox")
    assert [r["action"] for r in actions_log(web)] == ["restore", "restore"]


def test_a_row_still_in_junk_cannot_be_restored(web):
    status, _, _ = confirm_and_act(web, "restore-junk", "M4", "2026-11-05T10:00:03Z")
    assert status == 400 and web.graph.moves == []


def test_post_needs_the_token_and_the_same_origin(web):
    form = urlencode({"account": "live", "id": "M1", "ts": "2026-11-05T10:00:00Z", "action": "teach-spam", "token": "wrong"})
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    assert request(web, "POST", "/act", form, headers)[0] == 403
    good = urlencode({"account": "live", "id": "M1", "ts": "2026-11-05T10:00:00Z", "action": "teach-spam", "token": web.ctx.token})
    assert request(web, "POST", "/act", good, {**headers, "Origin": "http://evil.example"})[0] == 403
    assert web.learn == [] and actions_log(web) == []
    big = "x=" + "a" * 10000
    try:
        assert request(web, "POST", "/act", big, headers)[0] == 400
    except ConnectionError:
        pass  # the server refused and closed before the client finished sending: also a refusal


def test_without_the_job_actions_are_unavailable_but_viewing_works(tmp_path):
    log_dir = tmp_path / "logs"
    write(log_dir, "decisions.jsonl", [rec(1)])
    ctx = Context(log_dir, None)
    server = make_server(0, ctx, host="127.0.0.1")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        port = server.server_address[1]
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", "/")
        assert conn.getresponse().status == 200
        form = urlencode({"account": "live", "id": "M1", "ts": "2026-11-01T10:00:01Z", "action": "teach-spam", "token": ctx.token})
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("POST", "/act", form, {"Content-Type": "application/x-www-form-urlencoded"})
        assert conn.getresponse().status in (404, 503)
    finally:
        server.shutdown()
        server.server_close()
