"""The review page's log reader: filters, sorting, paging, rotated files, bad lines, what-if."""

import json
import os

from outlook_junk_filter.ui import logreader
from outlook_junk_filter.ui.logreader import Filters


def rec(n, **kw):
    base = {
        "ts": f"2026-11-0{1 + n % 9}T10:00:{n % 60:02d}Z",
        "message_id": f"M{n}", "internet_message_id": f"<{n}@x.invalid>", "received": "2026-11-01T00:00:00Z",
        "from_addr": f"sender{n}@example.invalid", "subject": f"subject {n}", "score": float(n), "rules": ["A"],
        "keep_rule_hit": False, "relays_untrusted": "", "first_untrusted_ip": "", "auth_results_present": True,
        "mode": "report-only", "decision": "keep", "reason": "below threshold", "action_error": "",
        "current_message_id": f"M{n}", "folder": "junkemail",
    }
    base.update(kw)
    return base


def write(log_dir, name, records, extra_lines=()):
    log_dir.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(r) for r in records] + list(extra_lines)
    (log_dir / name).write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_missing_or_empty_log_is_an_empty_state(tmp_path):
    page = logreader.query(tmp_path / "nope", Filters())
    assert page.rows == [] and page.total == 0
    (tmp_path / "decisions.jsonl").write_text("", encoding="utf-8")
    assert logreader.query(tmp_path, Filters()).total == 0
    assert logreader.header(tmp_path)["records"] == 0 and logreader.header(tmp_path)["age_seconds"] is None


def test_malformed_and_half_written_lines_are_skipped_and_counted(tmp_path):
    write(tmp_path, "decisions.jsonl", [rec(1), rec(2)], extra_lines=["{not json", '{"half": '])
    page = logreader.query(tmp_path, Filters())
    assert page.total == 2 and page.skipped == 2
    assert logreader.header(tmp_path)["skipped"] == 2


def test_newest_first_by_default_and_rotated_files_are_read_in_order(tmp_path):
    write(tmp_path, "decisions.jsonl.2", [rec(1, ts="2026-11-01T00:00:00Z")])
    write(tmp_path, "decisions.jsonl.1", [rec(2, ts="2026-11-02T00:00:00Z"), rec(3, ts="2026-11-03T00:00:00Z")])
    write(tmp_path, "decisions.jsonl", [rec(4, ts="2026-11-04T00:00:00Z")])
    newest = [r["message_id"] for r in logreader.query(tmp_path, Filters()).rows]
    assert newest == ["M4", "M3", "M2", "M1"]
    oldest = [r["message_id"] for r in logreader.query(tmp_path, Filters(), "time", "asc").rows]
    assert oldest == ["M1", "M2", "M3", "M4"]


def test_filters(tmp_path):
    records = [
        rec(1, decision="deleted", score=20.0), rec(2, decision="keep", score=1.0, subject="Newsletter"),
        rec(3, decision="keep", score=9.5, from_addr="Bob@Example.invalid"), rec(4, score=None, decision="error"),
    ]
    write(tmp_path, "decisions.jsonl", records)

    def ids(**kw):
        return sorted(r["message_id"] for r in logreader.query(tmp_path, Filters(**kw)).rows)

    assert ids(decision="deleted") == ["M1"]
    assert ids(score_min=5.0) == ["M1", "M3"]
    assert ids(score_min=2.0, score_max=10.0) == ["M3"]
    assert ids(q="newsletter") == ["M2"]  # subject, case-insensitive
    assert ids(q="bob@example") == ["M3"]  # sender
    assert ids(date_from="2026-11-03", date_to="2026-11-03") == sorted(
        r["message_id"] for r in records if r["ts"].startswith("2026-11-03")
    )


def test_paging_is_100_rows_and_sorting_by_score(tmp_path):
    write(tmp_path, "decisions.jsonl", [rec(n, ts=f"2026-11-01T10:{n // 60:02d}:{n % 60:02d}Z", score=float(n)) for n in range(250)])
    p1 = logreader.query(tmp_path, Filters(), "time", "desc", 1)
    p3 = logreader.query(tmp_path, Filters(), "time", "desc", 3)
    assert (len(p1.rows), len(p3.rows), p1.total) == (100, 50, 250)
    assert p1.rows[0]["message_id"] == "M249" and p3.rows[-1]["message_id"] == "M0"
    top = logreader.query(tmp_path, Filters(), "score", "desc", 1)
    assert [r["score"] for r in top.rows[:3]] == [249.0, 248.0, 247.0] and top.total == 250
    low = logreader.query(tmp_path, Filters(), "score", "asc", 3)
    assert [r["score"] for r in low.rows[:2]] == [200.0, 201.0]


def test_records_without_a_score_sort_last(tmp_path):
    write(tmp_path, "decisions.jsonl", [rec(1, score=None), rec(2, score=3.0), rec(3, score=-2.0)])
    assert [r["message_id"] for r in logreader.query(tmp_path, Filters(), "score", "desc").rows] == ["M2", "M3", "M1"]
    assert [r["message_id"] for r in logreader.query(tmp_path, Filters(), "score", "asc").rows] == ["M3", "M2", "M1"]


def test_find_and_header(tmp_path):
    write(tmp_path, "decisions.jsonl", [rec(1, ts="2026-11-01T00:00:00Z"), rec(2, ts="2026-11-02T00:00:00Z")])
    assert logreader.find(tmp_path, "M1", "2026-11-01T00:00:00Z")["subject"] == "subject 1"
    assert logreader.find(tmp_path, "M2", "2026-11-01T00:00:00Z") is None  # right id, wrong time
    info = logreader.header(tmp_path)
    assert (info["records"], info["oldest"], info["newest"]) == (2, "2026-11-01T00:00:00Z", "2026-11-02T00:00:00Z")


def test_summary_counts_per_day_and_a_histogram(tmp_path):
    write(tmp_path, "decisions.jsonl", [
        rec(1, ts="2026-11-01T00:00:00Z", score=0.5), rec(3, ts="2026-11-01T00:00:00Z", score=1.2, decision="deleted"),
        rec(2, ts="2026-11-02T00:00:00Z", score=-0.5), rec(4, ts="2026-11-02T00:00:00Z", score=None, decision="error"),
    ])
    data = logreader.summary(tmp_path)
    assert data["per_day"]["2026-11-01"] == {"keep": 1, "deleted": 1}
    assert "per_account" not in data
    assert data["histogram"] == {-1: 1, 0: 1, 1: 1}


def test_what_if_matches_a_hand_computed_fixture(tmp_path):
    write(tmp_path, "decisions.jsonl", [
        rec(1, score=2.0), rec(2, score=6.0), rec(3, score=9.9), rec(4, score=10.0), rec(5, score=50.0, keep_rule_hit=True),
        rec(6, score=None, decision="error"),
        rec(7, score=3.0, ts="2026-11-01T00:00:00Z"), rec(7, score=12.0, ts="2026-11-02T00:00:00Z"),  # latest record wins
    ])
    assert logreader.what_if(tmp_path, 6.0, 10.0) == {"kept": 2, "quarantined": 2, "deleted": 2, "allowlisted": 1, "total": 6}
    # with no delete threshold nothing is deleted; with neither, everything is kept
    assert logreader.what_if(tmp_path, 6.0, None)["deleted"] == 0
    assert logreader.what_if(tmp_path, None, None)["kept"] == 6


def test_viewing_opens_nothing_outside_the_log_directory(tmp_path, monkeypatch):
    log_dir = tmp_path / "logs"
    write(log_dir, "decisions.jsonl", [rec(1)])
    (tmp_path / "tokens").mkdir()
    (tmp_path / "tokens" / "token.json").write_text("SECRET", encoding="utf-8")
    opened = []
    real_open = open

    def spy(file, *a, **k):
        opened.append(os.fspath(file))
        return real_open(file, *a, **k)

    monkeypatch.setattr("builtins.open", spy)
    logreader.query(log_dir, Filters())
    logreader.header(log_dir)
    logreader.summary(log_dir)
    logreader.what_if(log_dir, 1.0, 5.0)
    assert opened and all(os.path.dirname(p).replace("\\", "/").endswith("logs") for p in opened)
