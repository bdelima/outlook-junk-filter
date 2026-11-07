"""The decision table from the build spec, with Graph and SpamAssassin mocked."""

from conftest import FakeDecisions, FakeGraph, make_config, msg, scorer
from outlook_junk_filter.graph import GraphError, NetworkError
from outlook_junk_filter.runner import Runner
from outlook_junk_filter.scoring import ScoreError


def run(cfg, graph, state, log, score_fn):
    decisions = FakeDecisions()
    summary = Runner(cfg, graph, state, decisions, log, score_fn).run()
    return summary, decisions.records


ACT = {"OJF_MODE": "act", "OJF_DELETE_MODE": "deleted-items", "OJF_DELETE_THRESHOLD": "10"}


def test_report_only_high_score_is_would_delete_and_makes_no_call(state, log):
    cfg = make_config(OJF_DELETE_THRESHOLD="10")
    graph = FakeGraph([msg(1)])
    _, records = run(cfg, graph, state, log, scorer([20.0]))
    assert records[0]["decision"] == "would-delete"
    assert graph.actions() == []


def test_act_deleted_items_moves_at_or_above_threshold_only(state, log):
    cfg = make_config(**ACT)
    graph = FakeGraph([msg(1), msg(2), msg(3)])
    _, records = run(cfg, graph, state, log, scorer([10.0, 9.9, 30.0]))
    assert [r["decision"] for r in records] == ["deleted", "keep", "deleted"]
    assert graph.actions() == [("move", "AAA1", "deleteditems"), ("move", "AAA3", "deleteditems")]


def test_act_permanent_uses_permanent_delete_and_no_move(state, log):
    cfg = make_config(**{**ACT, "OJF_DELETE_MODE": "permanent"})
    graph = FakeGraph([msg(1)])
    _, records = run(cfg, graph, state, log, scorer([15.0]))
    assert records[0]["decision"] == "permanently-deleted"
    assert graph.actions() == [("permanentDelete", "AAA1")]


def test_act_with_missing_or_invalid_settings_behaves_as_report_only(state, log, caplog):
    for env in (
        {"OJF_MODE": "act", "OJF_DELETE_THRESHOLD": "10"},  # no delete mode
        {"OJF_MODE": "act", "OJF_DELETE_MODE": "deleted-items"},  # no threshold
        {"OJF_MODE": "act", "OJF_DELETE_MODE": "shred", "OJF_DELETE_THRESHOLD": "10"},
        {"OJF_MODE": "act", "OJF_DELETE_MODE": "permanent", "OJF_DELETE_THRESHOLD": "lots"},
        {"OJF_MODE": "act", "OJF_DELETE_MODE": "permanent", "OJF_DELETE_THRESHOLD": "nan"},
    ):
        state.handled.clear()
        caplog.clear()
        cfg = make_config(**env)
        graph = FakeGraph([msg(1)])
        with caplog.at_level("ERROR", logger="ojf-test"):
            _, records = run(cfg, graph, state, log, scorer([99.0]))
        assert graph.actions() == [], env
        assert records[0]["mode"] == "report-only", env
        assert records[0]["decision"] in ("keep", "would-delete"), env
        assert any("ERROR" in r.message for r in caplog.records), env  # logged on every run


def test_unrecognised_mode_is_report_only_and_logged(state, log, caplog):
    cfg = make_config(OJF_MODE="yes-please", OJF_DELETE_MODE="permanent", OJF_DELETE_THRESHOLD="1")
    graph = FakeGraph([msg(1)])
    with caplog.at_level("ERROR", logger="ojf-test"):
        run(cfg, graph, state, log, scorer([99.0]))
    assert graph.actions() == []
    assert any("not report-only or act" in r.message for r in caplog.records)


def test_keep_rule_match_keeps_a_high_score_with_allowlist_reason(state, log):
    cfg = make_config(**ACT)
    graph = FakeGraph([msg(1)])
    _, records = run(cfg, graph, state, log, scorer([50.0], {50.0: ["FOO", "USER_IN_DEF_DKIM_WL_WELCOMELIST"]}))
    assert records[0]["decision"] == "keep"
    assert records[0]["reason"] == "allowlisted"
    assert records[0]["keep_rule_hit"] is True
    assert graph.actions() == []


def test_scoring_failure_keeps_retries_and_gives_up_after_three_attempts(state, log):
    cfg = make_config(**ACT)
    graph = FakeGraph([msg(1)])
    decisions = []
    for _ in range(3):
        _, records = run(cfg, graph, state, log, scorer([ScoreError("timed out")]))
        decisions.append(records[0])
    assert [d["decision"] for d in decisions] == ["error", "error", "keep"]
    assert "gave up after 3 attempts" in decisions[2]["reason"]
    assert graph.actions() == []
    assert state.retry == {}  # given up: no longer retried
    # a fourth run does nothing: the message is handled
    _, records = run(cfg, graph, state, log, scorer([]))
    assert records == []


def test_unscored_message_is_never_deleted_even_with_high_neighbours(state, log):
    cfg = make_config(**ACT)
    graph = FakeGraph([msg(1), msg(2)])
    _, records = run(cfg, graph, state, log, scorer([ScoreError("x"), 40.0]))
    assert [r["decision"] for r in records] == ["error", "deleted"]
    assert graph.actions() == [("move", "AAA2", "deleteditems")]
    assert "AAA1" in state.retry


def test_delta_link_saved_only_after_the_whole_pass(state, log):
    cfg = make_config(**ACT)
    graph = FakeGraph([msg(1), msg(2)], delta_link="DELTA-2")
    boom = RuntimeError("crash mid-pass")
    try:
        run(cfg, graph, state, log, scorer([1.0, boom]))
    except RuntimeError:
        pass
    assert state.delta_link is None  # crashed: the pass will replay
    # message 1 was handled before the crash, so the replay skips it
    state.save()
    _, records = run(cfg, graph, state, log, scorer([2.0]))
    assert len(records) == 1 and records[0]["message_id"] == "AAA2"
    assert state.delta_link == "DELTA-2"


def test_saved_delta_link_is_passed_to_graph(state, log):
    state.delta_link = "DELTA-OLD"
    graph = FakeGraph([])
    run(make_config(), graph, state, log, scorer([]))
    assert graph.calls[0] == ("delta", "DELTA-OLD")
    assert state.delta_link == "DELTA-NEW"


def test_action_failure_keeps_the_message_and_retries(state, log):
    cfg = make_config(**ACT)
    graph = FakeGraph([msg(1)])
    graph.fail_move = True
    _, records = run(cfg, graph, state, log, scorer([20.0]))
    assert records[0]["decision"] == "error"
    assert "HTTP 500" in records[0]["action_error"]
    assert "AAA1" in state.retry


def test_already_handled_messages_are_skipped(state, log):
    cfg = make_config()
    graph = FakeGraph([msg(1)])
    run(cfg, graph, state, log, scorer([1.0]))
    _, records = run(cfg, graph, state, log, scorer([]))
    assert records == []


def test_network_failure_propagates_and_does_not_advance_the_delta_link(state, log):
    cfg = make_config()
    graph = FakeGraph([msg(1)])
    graph.raw_errors["AAA1"] = NetworkError("down")
    try:
        run(cfg, graph, state, log, scorer([]))
        raise AssertionError("expected NetworkError")
    except NetworkError:
        pass
    assert state.delta_link is None


def test_message_that_vanished_is_skipped(state, log):
    cfg = make_config()
    graph = FakeGraph([msg(1)])
    graph.raw_errors["AAA1"] = GraphError(404, "gone")
    _, records = run(cfg, graph, state, log, scorer([]))
    assert records == []
    assert state.delta_link == "DELTA-NEW"


def test_records_carry_the_documented_fields_and_cut_subjects(state, log):
    cfg = make_config()
    graph = FakeGraph([msg(1, subject="S" * 300)])
    _, records = run(cfg, graph, state, log, scorer([3.0]))
    record = records[0]
    assert set(record) == {
        "ts", "message_id", "internet_message_id", "received", "from_addr", "subject",
        "score", "rules", "keep_rule_hit", "relays_untrusted", "first_untrusted_ip",
        "auth_results_present", "mode", "decision", "reason", "action_error", "current_message_id", "folder",
    }
    assert len(record["subject"]) == 120
    assert "body" not in record


def test_run_summary_has_counts_and_histogram(state, log):
    cfg = make_config(OJF_DELETE_THRESHOLD="10")
    graph = FakeGraph([msg(1), msg(2), msg(3)], folder_count=3)
    summary, _ = run(cfg, graph, state, log, scorer([-0.5, 2.2, 12.9]))
    assert summary["counts"] == {"keep": 2, "would-delete": 1}
    assert summary["histogram"] == {"-1": 1, "2": 1, "12": 1}
    assert summary["folder_count"] == 3


def test_a_short_first_walk_is_warned_about(state, log, caplog):
    cfg = make_config()
    graph = FakeGraph([msg(1), msg(2)], folder_count=570)
    with caplog.at_level("WARNING", logger="ojf-test"):
        run(cfg, graph, state, log, scorer([1.0, 2.0]))
    assert any("returned 2 messages but the Junk folder holds 570" in r.getMessage() for r in caplog.records)


def test_no_warning_when_the_walk_matches_the_folder_or_a_delta_link_is_saved(state, log, caplog):
    cfg = make_config()
    with caplog.at_level("WARNING", logger="ojf-test"):
        run(cfg, FakeGraph([msg(1)], folder_count=1), state, log, scorer([1.0]))
        state.delta_link = "SAVED"
        run(cfg, FakeGraph([msg(2)], folder_count=570), state, log, scorer([1.0]))
    assert not any("first delta walk" in r.getMessage() for r in caplog.records)


def test_a_restored_message_is_kept_with_reason_restored_and_not_rescored(state, log, tmp_path):
    from outlook_junk_filter.restored import RestoredList

    restored = RestoredList(tmp_path)
    restored.add("<m1@example.invalid>")
    cfg = make_config(**ACT)
    graph = FakeGraph([msg(1), msg(2)])
    decisions = FakeDecisions()
    Runner(cfg, graph, state, decisions, log, scorer([50.0]), restored=restored).run()
    assert [(r["message_id"], r["decision"], r["reason"]) for r in decisions.records] == [
        ("AAA1", "keep", "restored"),
        ("AAA2", "deleted", "score at or above the delete threshold"),
    ]
    assert decisions.records[0]["score"] is None  # never fetched or scored
    assert graph.actions() == [("move", "AAA2", "deleteditems")]
