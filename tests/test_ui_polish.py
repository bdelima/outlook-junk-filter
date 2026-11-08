"""Plain-English rules, the actions-taken column and indicators, allowlist batches and column sorting."""

# ruff: noqa: F811  (the imported web fixture is used as a test argument)

import re
from urllib.parse import urlencode

from outlook_junk_filter.ui import logreader
from outlook_junk_filter.ui.history import History
from outlook_junk_filter.ui.rules import Rules
from test_ui_batch import M1, M2, M3, post
from test_ui_logreader import rec, write
from test_ui_server import actions_log, request, web  # noqa: F401


def sa_dir(tmp_path):
    d = tmp_path / "sa"
    d.mkdir()
    (d / "20_test.cf").write_text(
        "describe SOME_NEW_RULE The message looks odd.\n"
        "score SOME_NEW_RULE 2.5\n"
        "describe GOOD_ONE It is a good sign\n"
        "score GOOD_ONE -1.5\n"
        "describe ZERO_RULE Informational only\n"
        "score ZERO_RULE 0\n"
        "score BAYES_99 0 0 3.5 3.5\n"
        "score DKIM_INVALID 0.1\n",
        encoding="utf-8",
    )
    return d


def test_rules_use_the_curated_phrase_first_then_spamassassins_own_text(tmp_path):
    rules = Rules([sa_dir(tmp_path)])
    assert rules.describe("DKIM_INVALID") == "the sender's DKIM signature doesn't check out"
    assert rules.describe("SOME_NEW_RULE") == "The message looks odd"  # the trailing period is dropped
    assert rules.describe("NOT_A_RULE") == ""
    assert rules.describe("OJF_MS_SCL_8") == "Microsoft rated it highly likely spam (SCL 8)"
    assert rules.describe("OJF_AI_2") == "the AI model rated it 2 out of 10"
    assert rules.detail("ZERO_RULE") == "Informational only" and rules.detail("HTML_MESSAGE").startswith("informational")


def test_explain_separates_against_and_in_favor_hides_noise_and_orders_by_score(tmp_path):
    rules = Rules([sa_dir(tmp_path)])
    names = ["HTML_MESSAGE", "ZERO_RULE", "GOOD_ONE", "SOME_NEW_RULE", "BAYES_99", "OJF_MS_SCL_8", "NOT_A_RULE", "DKIM_VALID"]
    result = rules.explain(names)
    assert result.against[0] == "Microsoft rated it highly likely spam (SCL 8)"  # this project's own evidence leads
    assert result.against[1:] == ["the learned filter is almost certain it is spam (99%+)", "The message looks odd"]
    assert set(result.favor) == {"It is a good sign", "the sender's DKIM signature is valid"}
    assert result.more == 0


def test_explain_caps_the_lists_and_counts_what_it_leaves_out(tmp_path):
    rules = Rules([sa_dir(tmp_path)])
    many = ["SPF_FAIL", "DKIM_INVALID", "BAYES_99", "URIBL_BLACK", "MIME_HTML_ONLY", "SUBJ_ALL_CAPS", "LOTS_OF_MONEY"]
    result = rules.explain(many)
    assert len(result.against) == 5 and result.more == 2


def test_a_missing_rule_directory_is_not_an_error(tmp_path):
    assert Rules([tmp_path / "nope"]).explain(["DKIM_INVALID"]).against == ["the sender's DKIM signature doesn't check out"]


def test_history_chips_follow_the_message_and_the_sender():
    history = History([
        {"action": "teach-spam", "message_id": "NEW1", "target": "spam", "ts": "t1"},
        {"action": "restore", "message_id": "NEW1", "target": "inbox", "ts": "t2"},
        {"action": "allowlist", "message_id": "", "target": "*@phish.example", "ts": "t3"},
        {"action": "allowlist", "message_id": "", "target": "evil@phish.example", "ts": "t4"},
    ])
    row = {"current_message_id": "NEW1", "from_addr": "Evil@Phish.example"}
    assert [c[0] for c in history.chips(row)] == ["Spam", "Restored · Inbox", "Address", "Domain"]
    assert history.chips({"current_message_id": "OTHER", "from_addr": "x@other.example"}) == []


def test_the_actions_column_and_the_score_bubble_show_on_the_decisions_page(web):
    post(web, [M1], "teach-spam")
    page = request(web, "GET", "/")[2]
    assert '<span class="chip spam" title="' in page and ">Spam</span>" in page
    assert 'class="tip"' in page and "Against it" in page and "In its favor" in page and "AI rating" in page
    assert ">none</span>" in page  # rows with no action


def test_the_ai_rating_and_reason_appear_in_the_bubble_escaped(web):
    write(web.ctx.log_dir, "decisions.jsonl.2", [rec(8, ai_score=9, ai_reason="<b>pill</b> spam", ts="2026-11-04T10:00:00Z")])
    page = request(web, "GET", "/")[2]
    assert "9 of 10: “&lt;b&gt;pill&lt;/b&gt; spam”" in page and "<b>pill</b>" not in page


def test_the_details_page_shows_what_was_done_and_what_was_not(web):
    query = "/message?" + urlencode({"id": "M1", "ts": "2026-11-05T10:00:00Z"})
    before = request(web, "GET", query)[2]
    assert "Teach Bayes" in before and "Taught Bayes" not in before and "Domain allowlisted" not in before
    post(web, [M1], "teach-ham")
    post(web, [M1], "allow-domain")
    after = request(web, "GET", query)[2]
    assert "Taught Bayes: not spam" in after and "Domain allowlisted (phish.example)" in after
    assert "Allowlist sender address" in after  # the address itself is still not done


def test_batch_allowlist_writes_the_entry_and_records_the_message(web):
    status, _, body = post(web, [M1], "allow-domain")
    assert status == 200 and "1 done, 0 not done" in body
    assert "whitelist_auth *@phish.example" in (web.config / "ojf-allowlist.cf").read_text(encoding="utf-8")
    entries = [a for a in actions_log(web) if a["action"] == "allowlist"]
    assert entries[0]["message_id"] == "NEW1" and entries[0]["target"] == "*@phish.example"


def test_batch_allowlist_of_the_same_domain_twice_is_skipped(web):
    write(web.ctx.log_dir, "decisions.jsonl.3", [rec(5, from_addr="evil@phish.example", ts="2026-11-03T10:00:00Z")])
    status, _, body = post(web, [M1, "2026-11-03T10:00:00Z|M5"], "allow-domain")
    assert status == 200 and "1 done, 1 not done" in body and "more than once" in body


def test_batch_allowlist_refuses_a_sender_that_is_not_a_plain_address(web):
    write(web.ctx.log_dir, "decisions.jsonl.4", [rec(6, from_addr="not an address", ts="2026-11-02T10:00:00Z")])
    status, _, body = post(web, ["2026-11-02T10:00:00Z|M6"], "allow-address")
    assert status == 200 and "0 done, 1 not done" in body and not (web.config / "ojf-allowlist.cf").exists()


def test_choosing_an_action_from_the_menu_leads_to_its_confirm_button(web):
    status, _, body = post(web, [M1, M2, M3], extra=[("choose", "restore-inbox")])
    assert status == 200 and "applies to 2 of 3 selected" in body and 'class="primary"' in body
    assert web.graph.moves == []  # nothing runs until the button is pressed


def test_columns_sort_by_sender_subject_scl_and_decision(tmp_path):
    log_dir = tmp_path / "logs"
    write(log_dir, "decisions.jsonl", [
        rec(1, from_addr="b@x.example", subject="Zed", ms_scl=5, decision="keep"),
        rec(2, from_addr="a@x.example", subject="Alpha", ms_scl=None, decision="deleted"),
        rec(3, from_addr="c@x.example", subject="mid", ms_scl=9, decision="quarantined"),
    ])

    def order(sort, direction):
        page = logreader.query(log_dir, logreader.Filters(), sort, direction)
        return [r["message_id"] for r in page.rows]

    assert order("sender", "asc") == ["M2", "M1", "M3"] and order("sender", "desc") == ["M3", "M1", "M2"]
    assert order("subject", "asc") == ["M2", "M3", "M1"]
    assert order("scl", "desc") == ["M3", "M1", "M2"] and order("scl", "asc") == ["M1", "M3", "M2"]  # no value sorts last
    assert order("decision", "asc") == ["M2", "M1", "M3"]


def test_the_header_links_sort_and_headings_stay_on_one_line(web):
    page = request(web, "GET", "/?sort=sender&order=asc")[2]
    assert "sort=sender" in page and "▴" in page
    css = request(web, "GET", "/static/style.css")[2]
    assert "white-space: nowrap" in css and "col-resize" in css
    assert re.search(r'<col data-w="\d+">', page)


def test_the_ui_script_is_served_with_the_resize_code(web):
    status, headers, body = request(web, "GET", "/static/ui.js")
    assert status == 200 and "ojf-column-widths" in body and headers["Content-Type"].startswith("text/javascript")


def test_a_missing_score_or_scl_shows_na_not_a_dash(web):
    write(web.ctx.log_dir, "decisions.jsonl.6", [rec(7, score=None, ms_scl=None, ts="2026-11-02T09:00:00Z")])
    page = request(web, "GET", "/")[2]
    assert ">N/A</span>" in page  # the score
    assert '<td class="num">N/A</td>' in page  # the SCL
    assert '<td class="num">-</td>' not in page
    csv = request(web, "GET", "/export.csv")[2]
    assert "subject 7,,,keep" in csv  # a missing score is an empty cell in the CSV (not a formula-escaped dash)


def test_report_only_decisions_are_explained_in_the_filter_and_the_table(web):
    page = request(web, "GET", "/")[2]
    assert "would-delete (report-only)" in page and "would-quarantine (report-only)" in page
    assert "it would have been deleted (nothing was changed)" in page
    write(web.ctx.log_dir, "decisions.jsonl.7", [rec(8, decision="would-delete", ts="2026-11-02T08:00:00Z")])
    assert '<td title="report-only: it would have been deleted (nothing was changed)">would-delete</td>' in request(web, "GET", "/")[2]


def test_the_pulldown_list_gets_real_colors(web):
    css = request(web, "GET", "/static/style.css")[2]
    assert "select option { background-color: Canvas; color: CanvasText; }" in css
