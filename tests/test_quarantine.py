"""The quarantine feature: the unit tests the spec lists, with the same synthetic fixtures."""

from datetime import datetime, timezone

from conftest import FakeDecisions, FakeGraph, make_config, msg, scorer
from outlook_junk_filter.runner import AccountRunner

NOW = datetime(2026, 11, 10, 12, 0, 0, tzinfo=timezone.utc)
ACT = {"OJF_MODE_LIVE": "act", "OJF_DELETE_MODE": "deleted-items"}


def run(cfg, graph, state, log, score_fn):
    decisions = FakeDecisions()
    AccountRunner("live", cfg, graph, state, decisions, log, score_fn, now=lambda: NOW).run()
    return decisions.records


def decisions_of(records):
    return [r["decision"] for r in records if r["score"] is not None]


def test_report_only_between_the_thresholds_is_would_quarantine_with_no_graph_call(state, log):
    cfg = make_config(OJF_QUARANTINE_THRESHOLD="6", OJF_DELETE_THRESHOLD="10")
    graph = FakeGraph([msg(1)])
    records = run(cfg, graph, state, log, scorer([7.0]))
    assert records[0]["decision"] == "would-quarantine"
    assert [c[0] for c in graph.calls] == ["delta"]  # not even a folder lookup


def test_act_between_the_thresholds_moves_to_the_quarantine_folder(state, log):
    cfg = make_config(**ACT, OJF_QUARANTINE_THRESHOLD="6", OJF_DELETE_THRESHOLD="10")
    graph = FakeGraph([msg(1), msg(2), msg(3)])
    records = run(cfg, graph, state, log, scorer([7.0, 12.0, 3.0]))
    assert decisions_of(records) == ["quarantined", "deleted", "keep"]
    assert graph.actions() == [("move", "AAA1", "ID-Quarantined"), ("move", "AAA2", "deleteditems")]
    assert records[0]["folder"] == "Quarantined" and records[0]["current_message_id"] == "MOVED-AAA1"
    assert records[1]["folder"] == "deleteditems" and records[1]["current_message_id"] == "MOVED-AAA2"
    assert records[2]["folder"] == "junkemail" and records[2]["current_message_id"] == "AAA3"


def test_allowlist_beats_both_thresholds(state, log):
    cfg = make_config(**ACT, OJF_QUARANTINE_THRESHOLD="6", OJF_DELETE_THRESHOLD="10")
    graph = FakeGraph([msg(1)])
    records = run(cfg, graph, state, log, scorer([50.0], {50.0: ["USER_IN_WHITELIST"]}))
    assert records[0]["decision"] == "keep" and records[0]["reason"] == "allowlisted"
    assert graph.actions() == []


def test_threshold_order_violation_behaves_as_report_only_and_logs(state, log, caplog):
    cfg = make_config(**ACT, OJF_QUARANTINE_THRESHOLD="10", OJF_DELETE_THRESHOLD="10")
    graph = FakeGraph([msg(1)])
    with caplog.at_level("ERROR", logger="ojf-test"):
        records = run(cfg, graph, state, log, scorer([20.0]))
    assert graph.actions() == []
    assert records[0]["mode"] == "report-only" and records[0]["decision"] == "would-delete"
    assert any("lower than" in r.message for r in caplog.records)


def test_act_with_only_a_quarantine_threshold_quarantines_and_never_deletes(state, log):
    cfg = make_config(**ACT, OJF_QUARANTINE_THRESHOLD="6")
    graph = FakeGraph([msg(1), msg(2)])
    records = run(cfg, graph, state, log, scorer([7.0, 500.0]))
    assert decisions_of(records) == ["quarantined", "quarantined"]
    assert all(call[2] == "ID-Quarantined" for call in graph.actions())


def test_quarantine_off_ignores_the_quarantine_threshold(state, log):
    cfg = make_config(**ACT, OJF_QUARANTINE_THRESHOLD="6", OJF_DELETE_THRESHOLD="10", OJF_QUARANTINE_ENABLED="off")
    graph = FakeGraph([msg(1)])
    records = run(cfg, graph, state, log, scorer([7.0]))
    assert records[0]["decision"] == "keep"
    assert graph.actions() == []
    assert not any(c[0] in ("find_folder", "create_folder", "old_messages") for c in graph.calls)


def test_folder_name_comes_from_the_setting_and_a_missing_folder_is_created_once(state, log):
    cfg = make_config(**ACT, OJF_QUARANTINE_THRESHOLD="6", OJF_QUARANTINE_FOLDER="Suspect mail")
    graph = FakeGraph([msg(1), msg(2)])
    run(cfg, graph, state, log, scorer([7.0, 8.0]))
    assert [c for c in graph.calls if c[0] == "create_folder"] == [("create_folder", "Suspect mail")]
    assert graph.actions() == [("move", "AAA1", "ID-Suspect mail"), ("move", "AAA2", "ID-Suspect mail")]


def test_an_existing_folder_of_that_name_is_reused(state, log):
    cfg = make_config(**ACT, OJF_QUARANTINE_THRESHOLD="6")
    graph = FakeGraph([msg(1)], folders={"Quarantined": "ID-EXISTING"})
    run(cfg, graph, state, log, scorer([7.0]))
    assert not any(c[0] == "create_folder" for c in graph.calls)
    assert graph.actions() == [("move", "AAA1", "ID-EXISTING")]


def test_a_folder_name_that_resolves_to_junk_or_another_system_folder_is_rejected(state, log, caplog):
    for name in ("Junk Email", "junkemail", "JUNKEMAIL", "Inbox", "deleteditems"):
        caplog.clear()
        cfg = make_config(**ACT, OJF_QUARANTINE_THRESHOLD="6", OJF_QUARANTINE_FOLDER=name)
        graph = FakeGraph([msg(1)])
        state.handled.clear()
        with caplog.at_level("ERROR", logger="ojf-test"):
            records = run(cfg, graph, state, log, scorer([7.0]))
        assert graph.actions() == [], name
        assert not any(c[0] == "create_folder" for c in graph.calls), name
        assert records[0]["mode"] == "report-only" and records[0]["decision"] == "would-quarantine", name
        assert any("system folder" in r.message or "resolves to" in r.message for r in caplog.records), name


def test_a_folder_that_resolves_to_a_system_folder_by_id_is_rejected(state, log):
    cfg = make_config(**ACT, OJF_QUARANTINE_THRESHOLD="6", OJF_QUARANTINE_FOLDER="Posteingang")
    graph = FakeGraph([msg(1)], folders={"Posteingang": "ID-INBOX"})  # a localized name for the Inbox
    run(cfg, graph, state, log, scorer([7.0]))
    assert graph.actions() == []


def test_old_quarantined_messages_are_deleted_young_ones_are_left(state, log):
    cfg = make_config(**ACT, OJF_QUARANTINE_THRESHOLD="6", OJF_QUARANTINE_RETENTION_DAYS="7")
    graph = FakeGraph([], folders={"Quarantined": "ID-EXISTING"})
    old = {**msg(8), "receivedDateTime": "2026-11-02T11:59:59Z"}  # 8 days old
    young = {**msg(9), "receivedDateTime": "2026-11-04T00:00:00Z"}  # 6.5 days old
    graph.quarantined = [old, young]
    records = run(cfg, graph, state, log, scorer([]))
    assert graph.calls[-2][0] == "old_messages" or any(c[0] == "old_messages" for c in graph.calls)
    assert [c for c in graph.calls if c[0] == "old_messages"][0][2] == "2026-11-03T12:00:00Z"
    assert graph.actions() == [("move", "AAA8", "deleteditems")]
    assert records[0]["decision"] == "deleted" and "retention" in records[0]["reason"]


def test_expiry_follows_the_delete_mode(state, log):
    cfg = make_config(OJF_MODE_LIVE="act", OJF_DELETE_MODE="permanent", OJF_QUARANTINE_THRESHOLD="6")
    graph = FakeGraph([], folders={"Quarantined": "ID-EXISTING"})
    graph.quarantined = [{**msg(8), "receivedDateTime": "2026-01-01T00:00:00Z"}]
    records = run(cfg, graph, state, log, scorer([]))
    assert graph.actions() == [("permanentDelete", "AAA8")]
    assert records[0]["decision"] == "permanently-deleted" and records[0]["current_message_id"] == ""


def test_a_message_already_past_retention_is_deleted_without_waiting(state, log):
    cfg = make_config(**ACT, OJF_QUARANTINE_THRESHOLD="6")
    old = {**msg(1), "receivedDateTime": "2026-10-01T00:00:00Z"}
    graph = FakeGraph([old])
    # once it is quarantined the folder exists and lists it
    original_move = graph.move

    def move_and_list(message_id, destination):
        new_id = original_move(message_id, destination)
        graph.quarantined.append({**old, "id": new_id})
        return new_id

    graph.move = move_and_list
    records = run(cfg, graph, state, log, scorer([7.0]))
    assert [r["decision"] for r in records] == ["quarantined", "deleted"]
    assert graph.actions()[-1] == ("move", "MOVED-AAA1", "deleteditems")


def test_expiry_does_nothing_in_report_only_or_when_quarantine_is_off(state, log):
    for env in ({"OJF_MODE_LIVE": "report-only"}, {**ACT, "OJF_QUARANTINE_ENABLED": "off", "OJF_DELETE_THRESHOLD": "10"}):
        cfg = make_config(**env, OJF_QUARANTINE_THRESHOLD="6")
        graph = FakeGraph([], folders={"Quarantined": "ID-EXISTING"})
        graph.quarantined = [{**msg(8), "receivedDateTime": "2026-01-01T00:00:00Z"}]
        run(cfg, graph, state, log, scorer([]))
        assert graph.actions() == []


def test_expiry_failure_is_logged_and_the_message_stays(state, log):
    cfg = make_config(**ACT, OJF_QUARANTINE_THRESHOLD="6")
    graph = FakeGraph([], folders={"Quarantined": "ID-EXISTING"})
    graph.quarantined = [{**msg(8), "receivedDateTime": "2026-01-01T00:00:00Z"}]
    graph.fail_move = True
    records = run(cfg, graph, state, log, scorer([]))
    assert records[0]["decision"] == "error" and records[0]["action_error"]


def test_quarantine_move_failure_keeps_the_message_and_retries(state, log):
    cfg = make_config(**ACT, OJF_QUARANTINE_THRESHOLD="6")
    graph = FakeGraph([msg(1)])
    graph.fail_move = True
    records = run(cfg, graph, state, log, scorer([7.0]))
    assert records[0]["decision"] == "error" and "AAA1" in state.retry
