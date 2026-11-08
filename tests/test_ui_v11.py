"""Deleting log rows, pruning the logs, the SpamWatch header and the stats endpoint."""

# ruff: noqa: F811  (the imported web fixture is used as a test argument)

import json
import os
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from conftest import make_config
from outlook_junk_filter.logs import DecisionLog, read_tombstones
from outlook_junk_filter.status import read_status, write_status
from outlook_junk_filter.ui import stats
from test_ai_startup import Checker, run_startup
from test_ui_batch import M1, M2, M3, post
from test_ui_logreader import rec, write
from test_ui_server import actions_log, request, web  # noqa: F401

FORM = {"Content-Type": "application/x-www-form-urlencoded"}


def stamp(delta):
    return (datetime.now(timezone.utc) + delta).strftime("%Y-%m-%dT%H:%M:%SZ")


def ids(web):
    page = request(web, "GET", "/")[2]
    return {m for m in ("M1", "M2", "M3", "M4") if f"id={m}&amp;" in page or f"id={m}&" in page}


def test_the_header_shows_the_name_the_mailbox_and_the_icon(web):
    write_status(web.state, account="bob.delima.us@gmail.com")
    page = request(web, "GET", "/")[2]
    assert ">SpamWatch<" in page and "bob.delima.us@gmail.com" in page and 'href="/static/icon.svg"' in page
    assert "<title>Decisions · SpamWatch</title>" in page and "outlook-junk-filter" not in page
    status, headers, svg = request(web, "GET", "/static/icon.svg")
    assert status == 200 and headers["Content-Type"] == "image/svg+xml" and svg.startswith("<svg")


def test_the_mailbox_address_is_escaped(web):
    write_status(web.state, account="<script>x</script>@evil.example")
    page = request(web, "GET", "/")[2]
    assert "<script>x" not in page and "&lt;script&gt;x" in page


def test_deleting_one_row_hides_it_and_touches_nothing_in_outlook(web):
    query = urlencode({"id": "M4", "ts": "2026-11-05T10:00:03Z", "action": "delete-row"})
    status, _, confirm = request(web, "GET", "/confirm?" + query)
    assert status == 200 and "not touched" in confirm
    token = web.ctx.token
    form = urlencode({"id": "M4", "ts": "2026-11-05T10:00:03Z", "action": "delete-row", "token": token})
    status, _, body = request(web, "POST", "/act", form, FORM)
    assert status == 200 and "Removed this row from the log" in body and 'href="/"' in body
    assert "M4" not in ids(web)
    assert web.graph.moves == [] and web.graph.raw_calls == [] and web.learn == []
    assert read_tombstones(web.ctx.log_dir) == {("M4", "2026-11-05T10:00:03Z")}


def test_deleting_in_a_batch_hides_the_rows_and_reports_counts(web):
    status, _, body = post(web, [M1, M2, M3], "delete-row")
    assert status == 200 and "Delete from log: 3 done, 0 not done" in body
    assert ids(web) == {"M4"}
    assert request(web, "GET", "/message?" + urlencode({"id": "M1", "ts": "2026-11-05T10:00:00Z"}))[0] == 404
    assert len([a for a in actions_log(web) if a["action"] == "delete-row"]) == 3


def test_the_batch_menu_and_confirm_page_offer_delete_with_a_warning(web):
    page = request(web, "GET", "/")[2]
    assert 'value="delete-row"' in page and 'data-n="all"' in page
    confirm = post(web, [M1, M2], extra=[("choose", "delete-row")])[2]
    assert "Remove 2 row(s) from the log?" in confirm and 'class="danger"' in confirm
    assert read_tombstones(web.ctx.log_dir) == set()  # nothing happens until the button is pressed


def test_a_repeated_row_is_deleted_once(web):
    status, _, body = post(web, [M1, M1], "delete-row")
    assert status == 200 and "1 done, 0 not done" in body


def test_prune_drops_old_rows_deleted_rows_and_old_actions_and_logging_continues(tmp_path):
    state = tmp_path / "state"
    log = DecisionLog(state)
    log.write(rec(1, ts=stamp(timedelta(days=-40))))
    log.write(rec(2, ts=stamp(timedelta(days=-5))))
    log.write(rec(3, ts=stamp(timedelta(days=-1))))
    logs = state / "logs"
    (logs / "decisions.jsonl.1").write_text(json.dumps(rec(4, ts=stamp(timedelta(days=-90)))) + "\n", encoding="utf-8")
    (logs / "deleted.jsonl").write_text(json.dumps({"message_id": "M3", "ts": rec(3, ts=stamp(timedelta(days=-1)))["ts"]}) + "\n", encoding="utf-8")
    old, fresh = stamp(timedelta(days=-60)), stamp(timedelta(days=-2))
    (logs / "actions.jsonl").write_text(
        "".join(json.dumps(a) + "\n" for a in (
            {"ts": old, "action": "teach-spam", "message_id": "x", "target": "spam"},
            {"ts": old, "action": "allowlist", "message_id": "", "target": "*@keep.example"},
            {"ts": fresh, "action": "restore", "message_id": "y", "target": "inbox"},
        )),
        encoding="utf-8",
    )
    gone = log.prune(30)
    assert gone == {"old": 2, "deleted": 1, "actions": 1}
    left = [json.loads(line)["message_id"] for line in (logs / "decisions.jsonl").read_text(encoding="utf-8").splitlines()]
    assert left == ["M2"] and not (logs / "decisions.jsonl.1").exists() and not (logs / "deleted.jsonl").exists()
    kept = [json.loads(line)["action"] for line in (logs / "actions.jsonl").read_text(encoding="utf-8").splitlines()]
    assert kept == ["allowlist", "restore"]
    log.write(rec(9, ts=stamp(timedelta(0))))  # the handler reopens the file; nothing goes to the replaced one
    assert [json.loads(line)["message_id"] for line in (logs / "decisions.jsonl").read_text(encoding="utf-8").splitlines()] == ["M2", "M9"]


def test_prune_with_zero_days_only_removes_deleted_rows(tmp_path):
    log = DecisionLog(tmp_path)
    log.write(rec(1, ts="2020-01-01T00:00:00Z"))
    log.write(rec(2, ts="2020-01-02T00:00:00Z"))
    (tmp_path / "logs" / "deleted.jsonl").write_text(json.dumps({"message_id": "M2", "ts": "2020-01-02T00:00:00Z"}) + "\n", encoding="utf-8")
    assert log.prune(0) == {"old": 0, "deleted": 1, "actions": 0}


def test_log_retention_setting_defaults_to_30_and_rejects_nonsense():
    assert make_config().log_retention_days == 30
    assert make_config(OJF_LOG_RETENTION_DAYS="0").log_retention_days == 0
    cfg = make_config(OJF_LOG_RETENTION_DAYS="soon")
    assert cfg.log_retention_days == 30 and any("OJF_LOG_RETENTION_DAYS" in p for p in cfg.problems)
    assert make_config(OJF_LOG_RETENTION_DAYS="-3").log_retention_days == 30


def test_the_job_records_its_status_and_prunes_at_startup(monkeypatch, tmp_path, caplog):
    write(tmp_path / "logs", "decisions.jsonl", [rec(1, ts="2020-01-01T00:00:00Z")])
    text = run_startup(monkeypatch, tmp_path, caplog, {}, Checker(("info", "x")))
    status = read_status(tmp_path)
    assert status["account"] == "me@example.invalid" and status["version"] and "last_scan" in status
    assert "Pruned the logs: 1 decision row(s)" in text
    assert (tmp_path / "logs" / "decisions.jsonl").read_text(encoding="utf-8") == ""


def seed_stats(tmp_path):
    logs = tmp_path / "state" / "logs"
    now = datetime.now(timezone.utc).astimezone()
    today = now.replace(hour=12, minute=0, second=0, microsecond=0)
    if today > now:
        today -= timedelta(days=1)
        # a message from "earlier today" can fall on yesterday just after midnight; use the current moment instead
    today = min(today, now - timedelta(seconds=5))
    start_of_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if today < start_of_today:
        today = now - timedelta(seconds=5)

    def iso(moment):
        return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    write(logs, "decisions.jsonl", [
        rec(1, ts=iso(now - timedelta(days=3)), decision="keep"),
        rec(2, ts=iso(now - timedelta(days=2)), decision="would-delete"),
        rec(3, ts=iso(today), decision="quarantined", ai_score=8),
        rec(3, ts=iso(today + timedelta(seconds=1)), decision="deleted", ai_score=9),  # a later pass on the same message
        rec(4, ts=iso(today), decision="keep"),
        rec(5, ts=iso(now - timedelta(days=20)), decision="deleted"),
        rec(6, ts=iso(today), decision="error"),
    ])
    (tmp_path / "state" / "health").write_text("", encoding="utf-8")
    write_status(tmp_path / "state", account="me@example.invalid", version="0.11.0", mode="act", last_scan=iso(now - timedelta(seconds=90)))
    return logs


def test_stats_count_the_latest_row_per_message_in_each_window(tmp_path):
    data = stats.compute(seed_stats(tmp_path))
    assert data["today"] == {"scored": 3, "kept": 1, "quarantined": 0, "deleted": 1, "errors": 1, "ai_rated": 1}
    assert data["last_7_days"]["scored"] == 5 and data["last_7_days"]["deleted"] == 2 and data["last_7_days"]["kept"] == 2
    assert data["log_records"] == 7
    assert data["account"] == "me@example.invalid" and data["mode"] == "act" and data["version"] == "0.11.0"
    assert data["healthy"] is True and 85 <= data["last_scan_seconds_ago"] <= 120


def test_stats_without_a_status_file_are_still_valid(tmp_path):
    logs = tmp_path / "state" / "logs"
    write(logs, "decisions.jsonl", [])
    data = stats.compute(logs)
    assert data["account"] is None and data["last_scan_seconds_ago"] is None and data["healthy"] is False and data["today"]["scored"] == 0


def test_the_stats_endpoint_serves_json_with_counts_only_and_caches(web):
    status, headers, body = request(web, "GET", "/api/stats")
    data = json.loads(body)
    assert status == 200 and headers["Content-Type"].startswith("application/json") and data["app"] == "SpamWatch"
    assert set(data) >= {"account", "mode", "healthy", "last_scan_seconds_ago", "today", "last_7_days", "log_records"}
    assert "example.invalid" not in body and "subject" not in body  # no senders or subjects
    write(web.ctx.log_dir, "decisions.jsonl.5", [rec(7, ts=stamp(timedelta(0)))])
    assert json.loads(request(web, "GET", "/api/stats")[2])["log_records"] == data["log_records"]  # served from the cache
    web.ctx.stats_cache = (time.monotonic() - 3600, web.ctx.stats_cache[1])
    assert json.loads(request(web, "GET", "/api/stats")[2])["log_records"] == data["log_records"] + 1


def test_deleting_rows_clears_the_stats_cache(web):
    request(web, "GET", "/api/stats")
    assert web.ctx.stats_cache[1] is not None
    post(web, [M1], "delete-row")
    assert web.ctx.stats_cache[1] is None


def test_status_file_merges_and_survives_garbage(tmp_path):
    write_status(tmp_path, account="a@b.example")
    write_status(tmp_path, version="1")
    assert read_status(tmp_path) == {"account": "a@b.example", "version": "1"}
    (tmp_path / "status.json").write_text("not json", encoding="utf-8")
    assert read_status(tmp_path) == {}
    assert os.path.exists(tmp_path / "status.json")
