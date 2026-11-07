"""Shared fakes: Graph, decision log, scorer. No network, no SpamAssassin, no real mail."""

from __future__ import annotations

import logging

import pytest

from outlook_junk_filter import config as config_module
from outlook_junk_filter.graph import GraphError
from outlook_junk_filter.scoring import ScoreResult
from outlook_junk_filter.state import AccountState


def make_config(**env):
    base = {"OJF_CLIENT_ID": "00000000-0000-0000-0000-000000000000"}
    base.update({k: str(v) for k, v in env.items()})
    return config_module.load(base)


def msg(n, subject="hello", internet_id=None):
    return {
        "id": f"AAA{n}",
        "internetMessageId": internet_id or f"<m{n}@example.invalid>",
        "receivedDateTime": "2026-11-06T10:00:00Z",
        "from": {"emailAddress": {"address": f"sender{n}@example.invalid"}},
        "subject": subject,
    }


class FakeGraph:
    def __init__(self, messages=(), delta_link="DELTA-NEW", folder_count=None):
        self.messages = list(messages)
        self.delta_link = delta_link
        self.folder_count = len(self.messages) if folder_count is None else folder_count
        self.raws = {}
        self.calls = []
        self.fail_move = False
        self.fail_perm = False
        self.raw_errors = {}  # message id -> exception to raise

    def folder(self):
        return {"displayName": "Junk Email", "totalItemCount": self.folder_count}

    def delta(self, saved):
        self.calls.append(("delta", saved))
        return list(self.messages), self.delta_link

    def raw(self, message_id):
        if message_id in self.raw_errors:
            raise self.raw_errors[message_id]
        return self.raws.get(message_id, b"Subject: x\r\n\r\nbody")

    def move_to_deleted_items(self, message_id):
        self.calls.append(("move", message_id))
        if self.fail_move:
            raise GraphError(500, "boom")

    def permanent_delete(self, message_id):
        self.calls.append(("permanentDelete", message_id))
        if self.fail_perm:
            raise GraphError(500, "boom")

    def actions(self):
        return [c for c in self.calls if c[0] in ("move", "permanentDelete")]


class FakeDecisions:
    def __init__(self):
        self.records = []

    def write(self, record):
        self.records.append(record)


def scorer(scores, rules=None):
    """A score function: scores is {raw-bytes-marker or message order -> score}; default per call list."""
    queue = list(scores)
    rules = rules or {}

    def score_fn(raw):
        value = queue.pop(0)
        if isinstance(value, Exception):
            raise value
        return ScoreResult(score=value, rules=list(rules.get(value, [])), auth_results_present=True)

    return score_fn


@pytest.fixture
def log():
    logger = logging.getLogger("ojf-test")
    logger.setLevel(logging.DEBUG)
    return logger


@pytest.fixture
def state(tmp_path):
    return AccountState("live", tmp_path)
