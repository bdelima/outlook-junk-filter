"""Delete every row a filter matches, and return to the filtered view afterwards."""

# ruff: noqa: F811  (the imported web fixture is used as a test argument)

import re
from urllib.parse import urlencode

from outlook_junk_filter.logs import read_tombstones
from test_ui_batch import M1, post
from test_ui_logreader import rec, write
from test_ui_server import actions_log, request, web  # noqa: F401

FORM = {"Content-Type": "application/x-www-form-urlencoded"}


def dm(web, fields, confirm=None, token=None):
    data = [("token", web.ctx.token if token is None else token)] + list(fields)
    if confirm is not None:
        data += [("confirm", "1"), ("expect", str(confirm))]
    return request(web, "POST", "/delete-matching", urlencode(data), FORM)


def test_the_button_shows_the_match_count_and_carries_the_filter(web):
    page = request(web, "GET", "/?decision=deleted")[2]
    assert "Delete all 1 matching" in page and 'form="delete-matching"' in page
    form = re.search(r'<form id="delete-matching".*?</form>', page, re.S).group(0)
    assert 'name="decision" value="deleted"' in form and web.ctx.token in form


def test_the_first_step_counts_and_confirms_without_deleting(web):
    status, _, body = dm(web, [("decision", "deleted")])
    assert status == 200 and "Remove 1 row(s) from the log?" in body and "decision: deleted" in body
    assert 'name="expect" value="1"' in body and read_tombstones(web.ctx.log_dir) == set()


def test_no_filter_says_it_means_every_row(web):
    body = dm(web, [])[2]
    assert "Remove 4 row(s)" in body and "every row in the log" in body


def test_confirming_removes_exactly_the_matching_rows_and_returns_to_the_filtered_view(web):
    status, _, body = dm(web, [("decision", "deleted")], confirm=1)
    assert status == 200 and "Removed 1 row(s) from the log." in body and 'href="/?decision=deleted"' in body
    assert read_tombstones(web.ctx.log_dir) == {("M2", "2026-11-05T10:00:01Z")}
    assert web.graph.moves == [] and web.graph.raw_calls == [] and web.learn == []
    assert "Delete all" not in request(web, "GET", "/?decision=deleted")[2] and "No decisions to show" in request(web, "GET", "/?decision=deleted")[2]
    assert "M1" in request(web, "GET", "/")[2]  # other rows are untouched


def test_a_changed_count_asks_again_instead_of_deleting(web):
    write(web.ctx.log_dir, "decisions.jsonl.7", [rec(9, decision="deleted", ts="2026-11-01T10:00:00Z")])
    status, _, body = dm(web, [("decision", "deleted")], confirm=1)  # the page showed 1; there are now 2
    assert status == 200 and "changed from 1 to 2" in body and read_tombstones(web.ctx.log_dir) == set()


def test_no_matches_says_so(web):
    assert "No rows match" in dm(web, [("q", "no-such-sender")])[2]


def test_it_needs_the_token_and_the_job(web, tmp_path):
    assert dm(web, [("decision", "deleted")], confirm=1, token="wrong")[0] == 403
    assert read_tombstones(web.ctx.log_dir) == set()


def test_a_large_delete_is_audited_with_one_line(web, monkeypatch):
    from outlook_junk_filter.ui import actions

    monkeypatch.setattr(actions, "AUDIT_LIMIT", 1)
    dm(web, [], confirm=4)
    entries = [a for a in actions_log(web) if a["action"].startswith("delete")]
    assert entries == [{**entries[0], "action": "delete-rows", "target": "4 rows"}]


def test_batch_results_return_to_the_filtered_and_sorted_view(web):
    page = request(web, "GET", "/?decision=quarantined&sort=score&order=asc&page=1")[2]
    assert 'name="return" value="decision=quarantined&amp;sort=score&amp;order=asc"' in page
    ret = urlencode({"decision": "quarantined", "sort": "score", "order": "asc", "zzz": "x", "page": "9"})
    step1 = request(web, "POST", "/batch", urlencode([("token", web.ctx.token), ("sel", M1), ("return", ret)]), FORM)[2]
    assert 'href="/?decision=quarantined&amp;sort=score&amp;order=asc"' in step1 and "zzz" not in step1 and "page=9" not in step1
    done = post(web, [M1], "teach-spam", extra=[("return", ret)])[2]
    assert 'href="/?decision=quarantined&amp;sort=score&amp;order=asc"' in done
