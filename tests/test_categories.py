"""Outlook category hooks: apply a category in Outlook, the next pass runs the matching action."""

import logging
import subprocess

import pytest

from conftest import FakeDecisions, FakeGraph, make_config, msg, scorer
from outlook_junk_filter import categories as cat
from outlook_junk_filter.graph import GraphError
from outlook_junk_filter.restored import RestoredList
from outlook_junk_filter.runner import Runner
from outlook_junk_filter.state import State
from outlook_junk_filter.ui.actions import Actions

OK = subprocess.CompletedProcess([], 0, b"", b"")
TICK = "✓"


class CatGraph(FakeGraph):
    def __init__(self, existing=(), **kw):
        super().__init__(**kw)
        self.existing = [{"displayName": n} for n in existing]
        self.by_folder = {}  # folder -> messages
        self.sets = []
        self.fail_set = False
        self.queries = []

    def master_categories(self):
        return list(self.existing)

    def create_master_category(self, name, color):
        self.calls.append(("create_category", name, color))

    def categorized_messages(self, folder, names):
        self.queries.append(folder)
        return list(self.by_folder.get(folder, []))

    def set_categories(self, message_id, categories):
        if self.fail_set:
            raise GraphError(500, "boom")
        self.sets.append((message_id, list(categories)))


def tagged(n, *categories, sender=None):
    m = msg(n)
    m["categories"] = list(categories)
    if sender:
        m["from"] = {"emailAddress": {"address": sender}}
    return m


def created(graph):
    return [c[1] for c in graph.calls if c[0] == "create_category"]


@pytest.fixture
def env(tmp_path):
    state, config = tmp_path / "state", tmp_path / "config"
    config.mkdir()
    graph = CatGraph(existing=cat.wanted_categories())
    learned = []

    def run(cmd, **kw):
        learned.append(cmd[1])
        return OK

    actions = Actions(graph, RestoredList(state), state, config, run=run)
    hooks = cat.CategoryHooks(make_config(), graph, actions, logging.getLogger("t"))
    return type("E", (), {"graph": graph, "hooks": hooks, "learned": learned, "config": config, "state": state, "actions": actions})


def test_there_are_thirteen_categories_with_the_decided_names_and_colors():
    want = cat.wanted_categories()
    assert len(want) == 13
    assert {"Spam", "Not Spam", "Allow Sender", "Allow Domain", "Restore to Junk", "Conflict", "Failure"} <= set(want)
    assert want["Spam " + TICK] == "Preset4" and want["Conflict"] == "Preset0" and want["Deleted " + TICK] == "Preset12"


def test_missing_categories_are_created_once_and_existing_ones_are_left_alone(env):
    env.graph.existing = [{"displayName": "spam"}, {"displayName": "Conflict"}]
    env.hooks.run()
    assert len(created(env.graph)) == 11 and "Spam" not in created(env.graph) and "Conflict" not in created(env.graph)
    env.hooks.run()
    assert len(created(env.graph)) == 11


def test_a_failed_setup_is_retried_on_the_next_pass(env):
    env.graph.existing = []
    original = env.graph.create_master_category

    def boom(name, color):
        raise GraphError(403, "no scope")

    env.graph.create_master_category = boom
    with pytest.raises(GraphError):
        env.hooks.run()
    env.graph.create_master_category = original
    env.hooks.run()
    assert len(created(env.graph)) == 13


def test_spam_teaches_bayes_swaps_the_category_for_its_marker_and_keeps_other_categories(env):
    env.graph.by_folder["junkemail"] = [tagged(1, "Spam", "Red category", "Not Spam " + TICK)]
    assert env.hooks.run() == {"done": 1}
    assert env.learned == ["--spam"]
    assert env.graph.sets == [("AAA1", ["Red category", "Spam " + TICK])]
    assert env.graph.queries == ["junkemail", "deleteditems"]


def test_not_spam_teaches_ham_and_retires_the_spam_marker(env):
    env.graph.by_folder["deleteditems"] = [tagged(2, "Not Spam", "Spam " + TICK)]
    env.hooks.run()
    assert env.learned == ["--ham"]
    assert env.graph.sets == [("AAA2", ["Not Spam " + TICK])]


def test_allow_sender_and_allow_domain_write_the_allowlist_and_are_recorded(env):
    env.graph.by_folder["junkemail"] = [
        tagged(1, "Allow Sender", sender="a@good.example"),
        tagged(2, "Allow Domain", sender="b@fine.example"),
    ]
    env.hooks.run()
    text = (env.config / "ojf-allowlist.cf").read_text()
    assert "whitelist_auth a@good.example" in text and "whitelist_auth *@fine.example" in text
    assert env.graph.sets == [("AAA1", ["Allow Sender " + TICK]), ("AAA2", ["Allow Domain " + TICK])]
    assert (env.state / "logs" / "actions.jsonl").read_text().count('"allowlist"') == 2


def test_restore_from_deleted_items_moves_to_junk_and_marks_the_moved_message(env):
    env.graph.by_folder["deleteditems"] = [tagged(3, "Restore to Junk", "Deleted " + TICK)]
    env.hooks.run()
    assert ("move", "AAA3", "junkemail") in env.graph.calls
    assert env.graph.sets == [("MOVED-AAA3", ["Restore to Junk " + TICK])]  # on the id the move returned; no longer "Deleted"
    assert env.actions._restored.contains("<m3@example.invalid>")


def test_restore_from_the_quarantine_folder_is_looked_for_too(env):
    env.graph.folders["Quarantined"] = "ID-Q"
    env.graph.by_folder["ID-Q"] = [tagged(4, "Restore to Junk")]
    env.hooks.run()
    assert "ID-Q" in env.graph.queries
    assert ("move", "AAA4", "junkemail") in env.graph.calls


def test_restore_on_a_message_already_in_junk_only_removes_the_category(env):
    env.graph.by_folder["junkemail"] = [tagged(5, "Restore to Junk")]
    env.hooks.run()
    assert not [c for c in env.graph.calls if c[0] == "move"]
    assert env.graph.sets == [("AAA5", [])]


def test_two_action_categories_do_nothing_and_get_conflict_once(env):
    env.graph.by_folder["junkemail"] = [tagged(1, "Spam", "Not Spam")]
    assert env.hooks.run() == {"conflict": 1}
    assert env.learned == [] and env.graph.sets == [("AAA1", ["Spam", "Not Spam", "Conflict"])]
    env.graph.by_folder["junkemail"] = [tagged(1, "Spam", "Not Spam", "Conflict")]
    env.graph.sets.clear()
    env.hooks.run()
    assert env.graph.sets == [] and env.learned == []


def test_a_leftover_conflict_is_cleared_and_the_single_action_runs(env):
    env.graph.by_folder["junkemail"] = [tagged(1, "Spam", "Conflict")]
    env.hooks.run()
    assert env.learned == ["--spam"] and env.graph.sets == [("AAA1", ["Spam " + TICK])]


def test_a_failed_action_leaves_the_category_adds_failure_and_is_not_retried(env):
    env.graph.by_folder["deleteditems"] = [tagged(1, "Not Spam")]
    env.graph.raw_errors["AAA1"] = GraphError(404, "gone")
    assert env.hooks.run() == {"failed": 1}
    assert env.graph.sets == [("AAA1", ["Not Spam", "Failure"])]
    env.graph.by_folder["deleteditems"] = [tagged(1, "Not Spam", "Failure")]
    env.graph.raw_errors.clear()
    env.graph.sets.clear()
    env.hooks.run()
    assert env.learned == [] and env.graph.sets == []


def test_failure_is_removed_by_a_later_success(env):
    env.graph.by_folder["junkemail"] = [tagged(1, "Spam", "Failure")]
    env.hooks.run()
    assert env.learned == []  # Failure blocks it until the person clears Failure
    env.graph.by_folder["junkemail"] = [tagged(1, "Spam")]
    env.hooks.run()
    assert env.graph.sets[-1] == ("AAA1", ["Spam " + TICK])


def test_the_inbox_is_never_queried_even_if_the_quarantine_name_points_at_it(tmp_path):
    graph = CatGraph(existing=cat.wanted_categories())
    graph.folders["Archive2"] = "ID-INBOX"  # a folder name that resolves to the Inbox by id
    actions = Actions(graph, RestoredList(tmp_path / "s"), tmp_path / "s", tmp_path)
    hooks = cat.CategoryHooks(make_config(OJF_QUARANTINE_FOLDER="Archive2"), graph, actions, logging.getLogger("t"))
    hooks.run()
    assert graph.queries == ["junkemail", "deleteditems"]
    graph.queries.clear()
    hooks = cat.CategoryHooks(make_config(OJF_QUARANTINE_FOLDER="Inbox"), graph, actions, logging.getLogger("t"))
    hooks.run()
    assert graph.queries == ["junkemail", "deleteditems"]


def test_actions_run_in_report_only_mode_too(env):
    assert env.hooks.cfg.effective_mode() == "report-only"
    env.graph.by_folder["junkemail"] = [tagged(1, "Spam")]
    env.hooks.run()
    assert env.learned == ["--spam"]


def test_a_category_update_that_fails_does_not_stop_the_pass(env):
    env.graph.fail_set = True
    env.graph.by_folder["junkemail"] = [tagged(1, "Spam"), tagged(2, "Not Spam")]
    env.hooks.run()
    assert env.learned == ["--spam", "--ham"]


# ---------------------------------------------------------------------- Deleted marker


def run_delete(tmp_path, delete_mode="deleted-items", **graph_flags):
    graph = FakeGraph([{**msg(1), "categories": ["Blue category"]}])
    for key, value in graph_flags.items():
        setattr(graph, key, value)
    cfg = make_config(OJF_MODE="act", OJF_DELETE_THRESHOLD="5", OJF_DELETE_MODE=delete_mode)
    records = FakeDecisions()
    Runner(cfg, graph, State(tmp_path), records, logging.getLogger("t"), scorer([9.0])).run()
    return graph, records.records


def test_a_message_the_job_deletes_gets_the_deleted_category(tmp_path):
    graph, records = run_delete(tmp_path)
    assert ("categories", "MOVED-AAA1", ["Blue category", "Deleted " + TICK]) in graph.calls
    assert records[0]["decision"] == "deleted" and records[0]["current_message_id"] == "MOVED-AAA1"


def test_a_failed_tag_never_undoes_or_fails_the_delete(tmp_path):
    _, records = run_delete(tmp_path, fail_tag=True)
    assert records[0]["decision"] == "deleted"


def test_a_permanent_delete_has_nothing_to_tag(tmp_path):
    graph, records = run_delete(tmp_path, delete_mode="permanent")
    assert records[0]["decision"] == "permanently-deleted"
    assert not [c for c in graph.calls if c[0] == "categories"]
