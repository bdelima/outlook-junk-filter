import json
import os
import time
from datetime import datetime, timedelta, timezone

from outlook_junk_filter import healthcheck
from outlook_junk_filter.logs import DecisionLog, histogram, touch_health
from outlook_junk_filter.state import AccountState


def test_state_round_trip_and_90_day_pruning(tmp_path):
    now = datetime(2026, 11, 7, tzinfo=timezone.utc)
    st = AccountState("live", tmp_path, clock=lambda: now)
    st.delta_link = "D"
    st.handled["<new@x>"] = "2026-11-01T00:00:00Z"
    st.handled["<old@x>"] = (now - timedelta(days=91)).strftime("%Y-%m-%dT%H:%M:%SZ")
    st.add_retry({"id": "1"}, "scoring failed")
    st.save()
    again = AccountState("live", tmp_path, clock=lambda: now)
    assert again.delta_link == "D"
    assert "<new@x>" in again.handled and "<old@x>" not in again.handled
    assert again.retry["1"]["attempts"] == 1


def test_corrupt_state_file_starts_clean(tmp_path):
    path = tmp_path / "delta"
    path.mkdir()
    (path / "live.json").write_text("{not json", encoding="utf-8")
    assert AccountState("live", tmp_path).delta_link is None


def test_messages_without_internet_id_are_keyed_by_graph_id(tmp_path):
    st = AccountState("live", tmp_path)
    st.mark_handled({"id": "abc"})
    assert st.is_handled({"id": "abc"}) and not st.is_handled({"id": "other"})


def test_decision_log_writes_one_json_object_per_line(tmp_path):
    log = DecisionLog(tmp_path)
    log.write({"a": 1, "subject": "é"})
    log.write({"a": 2})
    lines = (tmp_path / "logs" / "decisions.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["a"] for line in lines] == [1, 2]


def test_histogram_uses_whole_number_buckets():
    assert histogram([-0.1, 0.0, 0.9, 1.0, 5.5]) == {"-1": 1, "0": 2, "1": 1, "5": 1}


def test_health_file_and_check(tmp_path):
    assert healthcheck.healthy(tmp_path / "health") is False
    touch_health(tmp_path)
    assert healthcheck.healthy(tmp_path / "health") is True
    old = time.time() - 3 * 3600
    os.utime(tmp_path / "health", (old, old))
    assert healthcheck.healthy(tmp_path / "health") is False


def test_health_window_is_the_larger_of_three_intervals_and_30_minutes():
    assert healthcheck.window({"OJF_INTERVAL_SECONDS": "300"}) == 30 * 60
    assert healthcheck.window({"OJF_INTERVAL_SECONDS": "900"}) == 3 * 900
    assert healthcheck.window({"OJF_INTERVAL_SECONDS": "3600"}) == 3 * 3600
    assert healthcheck.window({}) == 3 * 900
    assert healthcheck.window({"OJF_INTERVAL_SECONDS": "junk"}) == 3 * 900
