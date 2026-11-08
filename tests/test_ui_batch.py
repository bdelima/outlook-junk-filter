"""Batch actions on the Decisions page: tick rows, choose teach or restore, see what was done."""

# ruff: noqa: F811  (the imported web fixture is used as a test argument)

import http.client
import re
import threading
from urllib.parse import urlencode

from outlook_junk_filter.ui.server import Context, make_server
from test_ui_logreader import rec, write
from test_ui_server import actions_log, request, web  # noqa: F401

FORM = {"Content-Type": "application/x-www-form-urlencoded"}
M1 = "2026-11-05T10:00:00Z|M1"  # quarantined: teachable and restorable
M2 = "2026-11-05T10:00:01Z|M2"  # in Deleted Items: teachable and restorable
M3 = "2026-11-05T10:00:02Z|M3"  # permanently deleted: neither
M4 = "2026-11-05T10:00:03Z|M4"  # still in Junk: teachable only


def post(web, selected, action=None, token=None, extra=()):
    fields = [("token", web.ctx.token if token is None else token)] + [("sel", s) for s in selected] + list(extra)
    if action:
        fields.append(("action", action))
    return request(web, "POST", "/batch", urlencode(fields), FORM)


def test_the_decisions_page_has_a_tick_box_only_on_rows_an_action_could_apply_to(web):
    body = request(web, "GET", "/")[2]
    boxes = re.findall(r'name="sel" value="([^"]+)"', body)
    assert set(boxes) == {M1, M2, M4}  # M3 is permanently deleted
    assert 'id="select-all"' in body and 'action="/batch"' in body and web.ctx.token in body
    assert request(web, "GET", "/static/batch.js")[0] == 200


def test_the_first_step_lists_the_selection_and_the_actions_with_counts_and_changes_nothing(web):
    status, _, body = post(web, [M1, M2, M3, M4])
    assert status == 200 and "4 message(s) selected" in body
    assert "Teach Bayes: spam (3 of 4)" in body and "Teach Bayes: not spam (3 of 4)" in body
    assert "Restore to Junk (2 of 4)" in body and "Restore to Inbox (2 of 4)" in body
    assert web.learn == [] and web.graph.moves == [] and actions_log(web) == []


def test_only_the_actions_that_apply_are_offered(web):
    body = post(web, [M4])[2]
    assert "Teach Bayes: spam (1 of 1)" in body and "Restore" not in body
    assert "None of the selected" in post(web, [M3])[2]


def test_teaching_a_batch_skips_what_does_not_apply_and_reports_it(web):
    status, _, body = post(web, [M1, M2, M3], "teach-spam")
    assert status == 200 and "Teach Bayes: spam: 2 done, 1 not done" in body
    assert len(web.learn) == 2 and web.graph.raw_calls == ["NEW1", "NEW2"]
    assert [a["action"] for a in actions_log(web)] == ["teach-spam", "teach-spam"]
    assert "Skipped" in body


def test_restoring_a_batch_moves_only_what_can_be_restored_and_tells_the_job_to_keep_it(web):
    status, _, body = post(web, [M1, M2, M4], "restore-inbox")
    assert status == 200 and "Restore to Inbox: 2 done, 1 not done" in body
    assert web.graph.moves == [("NEW1", "inbox"), ("NEW2", "inbox")]


def test_one_failure_does_not_stop_the_rest(web):
    web.graph.gone = True
    status, _, body = post(web, [M1, M2], "teach-ham")
    assert status == 200 and "0 done, 2 not done" in body and "no longer in Outlook" in body
    assert web.learn == []


def test_a_batch_needs_the_token_and_the_same_origin(web):
    assert post(web, [M1], "teach-spam", token="wrong")[0] == 403
    good = urlencode([("token", web.ctx.token), ("sel", M1), ("action", "teach-spam")])
    assert request(web, "POST", "/batch", good, {**FORM, "Origin": "http://evil.example"})[0] == 403
    assert web.learn == [] and actions_log(web) == []


def test_bad_selections_are_refused(web):
    assert post(web, [])[0] == 400
    assert post(web, ["no-separator"])[0] == 400
    assert post(web, [M1], "teach-everything")[0] == 400
    assert post(web, [f"2026-11-05T10:00:00Z|X{i}" for i in range(201)])[0] == 400
    status, _, body = post(web, [M1, "2026-01-01T00:00:00Z|NOPE"])
    assert status == 200 and "1 message(s) selected" in body and "no longer in the log" in body
    assert web.learn == []


def test_a_repeated_row_is_acted_on_once(web):
    status, _, body = post(web, [M1, M1], "teach-spam")
    assert status == 200 and "1 done, 0 not done" in body and len(web.learn) == 1


def test_two_log_rows_for_one_message_teach_it_once(web):
    again = {**rec(1), "ts": "2026-11-05T10:05:00Z", "current_message_id": "NEW1", "folder": "Quarantined"}
    write(web.ctx.log_dir, "decisions.jsonl.1", [again])
    status, _, body = post(web, [M1, "2026-11-05T10:05:00Z|M1"], "teach-spam")
    assert status == 200 and "1 done, 1 not done" in body and "more than once" in body
    assert web.graph.raw_calls == ["NEW1"]


def test_without_the_job_there_are_no_tick_boxes_and_no_batch(tmp_path):
    log_dir = tmp_path / "logs"
    write(log_dir, "decisions.jsonl", [rec(1)])
    ctx = Context(log_dir, None)
    server = make_server(0, ctx, host="127.0.0.1")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        port = server.server_address[1]
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", "/")
        body = conn.getresponse().read().decode()
        assert 'name="sel"' not in body and "/batch" not in body
        form = urlencode([("token", ctx.token), ("sel", "2026-11-01T10:00:01Z|M1"), ("action", "teach-spam")])
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("POST", "/batch", form, FORM)
        assert conn.getresponse().status == 503
    finally:
        server.shutdown()
        server.server_close()
