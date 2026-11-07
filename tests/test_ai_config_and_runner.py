"""AI scoring settings, and how the runner uses the AI: range, allowlist, points, failures."""

from conftest import FakeGraph, make_config, msg
from outlook_junk_filter.ai import AiError, AiResult
from outlook_junk_filter.runner import Runner
from outlook_junk_filter.scoring import ScoreResult

URL = "http://ojochal:8000"


def load(**env):
    return make_config(**env)


# --------------------------------------------------------------------- settings


def test_ai_scoring_is_off_by_default():
    cfg = load()
    assert cfg.ai_url is None and cfg.ai_min_score == 0.0 and cfg.ai_max_points == 4.0 and cfg.ai_timeout == 60
    assert cfg.describe()["ai_url"] is None and cfg.describe()["ai_range"] is None


def test_ai_url_accepts_a_bare_host_and_port_or_a_full_path():
    assert load(OJF_AI_URL=URL).ai_url == "http://ojochal:8000/v3"
    assert load(OJF_AI_URL=URL + "/").ai_url == "http://ojochal:8000/v3"
    assert load(OJF_AI_URL="http://ojochal:9000/v1/").ai_url == "http://ojochal:9000/v1"
    assert load(OJF_AI_URL="https://llm.example.invalid").ai_url == "https://llm.example.invalid/v3"
    for bad in ("ojochal:8000", "ftp://x", "http://", "not a url"):
        cfg = load(OJF_AI_URL=bad)
        assert cfg.ai_url is None and any("OJF_AI_URL" in p for p in cfg.problems), bad


def test_ai_range_defaults_to_zero_up_to_the_delete_threshold():
    cfg = load(OJF_AI_URL=URL, OJF_DELETE_THRESHOLD="6")
    assert (cfg.ai_min_score, cfg.ai_upper) == (0.0, 6.0)
    cfg = load(OJF_AI_URL=URL, OJF_DELETE_THRESHOLD="6", OJF_AI_MIN_SCORE="-1", OJF_AI_MAX_SCORE="4.5")
    assert (cfg.ai_min_score, cfg.ai_upper) == (-1.0, 4.5)
    assert load(OJF_AI_URL=URL).ai_upper is None  # no delete threshold, no limit


def test_ai_points_start_above_5_and_reach_the_maximum_at_10():
    cfg = load(OJF_AI_URL=URL)
    assert [cfg.ai_points(s) for s in (0, 5, 6, 7.5, 10)] == [0.0, 0.0, 0.8, 2.0, 4.0]
    assert load(OJF_AI_URL=URL, OJF_AI_MAX_POINTS="2").ai_points(10) == 2.0


def test_ai_bad_values_fall_back_and_are_reported():
    cfg = load(
        OJF_AI_URL=URL, OJF_AI_MAX_POINTS="lots", OJF_AI_MIN_SCORE="x", OJF_AI_MAX_SCORE="nan", OJF_AI_TIMEOUT_SECONDS="0"
    )
    assert (cfg.ai_max_points, cfg.ai_min_score, cfg.ai_max_score, cfg.ai_timeout) == (4.0, 0.0, None, 60)
    assert sum("OJF_AI_" in p for p in cfg.problems) == 4
    assert load(OJF_AI_URL=URL, OJF_AI_MAX_POINTS="-1").ai_max_points == 4.0


# --------------------------------------------------------------------- the runner


class FakeAi:
    def __init__(self, ratings):
        self.ratings = list(ratings)  # AiResult, or an AiError to raise
        self.calls = []

    def rate(self, sender, subject, text):
        self.calls.append((sender, subject, text))
        item = self.ratings.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def sa_score(base, scl=None, rules=()):
    def score_fn(raw):
        return ScoreResult(score=base, rules=list(rules), auth_results_present=True, ms_scl=scl)

    return score_fn


def run(cfg, messages, ai, score_fn, state, log, graph=None):
    from conftest import FakeDecisions

    decisions = FakeDecisions()
    graph = graph or FakeGraph(messages)
    runner = Runner(cfg, graph, state, decisions, log, score_fn, ai=ai)
    summary = runner.run()
    return summary, decisions.records, graph


ACT_CFG = {"OJF_MODE": "act", "OJF_DELETE_MODE": "deleted-items", "OJF_QUARANTINE_THRESHOLD": "3", "OJF_DELETE_THRESHOLD": "6"}


def test_a_message_in_range_is_rated_and_a_high_rating_adds_points_and_the_reason_is_stored(state, log):
    cfg = load(OJF_AI_URL=URL, **ACT_CFG)
    ai = FakeAi([AiResult(10.0, "pill spam")])
    _, records, graph = run(cfg, [msg(1, subject="Free pills")], ai, sa_score(1.2), state, log)
    record = records[0]
    assert record["score"] == 5.2 and record["decision"] == "quarantined"
    assert record["ai_score"] == 10.0 and record["ai_reason"] == "pill spam" and "OJF_AI_10" in record["rules"]
    assert ai.calls[0][1] == "Free pills" and ai.calls[0][0] == "sender1@example.invalid"
    assert "Subject: x" in ai.calls[0][2] or ai.calls[0][2] == "body"  # the text comes from the fetched message


def test_the_ai_can_push_a_quarantine_candidate_over_the_delete_line(state, log):
    cfg = load(OJF_AI_URL=URL, **ACT_CFG)
    _, records, _ = run(cfg, [msg(1)], FakeAi([AiResult(10.0, "scam")]), sa_score(4.0), state, log)
    assert records[0]["score"] == 8.0 and records[0]["decision"] == "deleted"


def test_a_low_rating_is_stored_but_adds_nothing(state, log):
    cfg = load(OJF_AI_URL=URL, **ACT_CFG)
    _, records, _ = run(cfg, [msg(1)], FakeAi([AiResult(2.0, "looks like a statement")]), sa_score(1.2), state, log)
    assert records[0]["score"] == 1.2 and records[0]["ai_score"] == 2.0 and "OJF_AI_2" not in records[0]["rules"]


def test_messages_outside_the_range_are_not_sent_to_the_ai(state, log, tmp_path):
    cfg = load(OJF_AI_URL=URL, OJF_AI_MIN_SCORE="0", **ACT_CFG)
    ai = FakeAi([])
    for i, base in enumerate((-1.5, 6.0, 9.0)):  # below the minimum, at the delete threshold, above it
        run(cfg, [msg(1)], ai, sa_score(base), type(state)(tmp_path / f"s{i}"), log)
    assert ai.calls == []


def test_the_range_includes_the_minimum_and_stops_below_the_upper_limit(state, log, tmp_path):
    cfg = load(OJF_AI_URL=URL, OJF_AI_MIN_SCORE="0", OJF_AI_MAX_SCORE="4", **ACT_CFG)
    ai = FakeAi([AiResult(1.0, ""), AiResult(1.0, "")])
    for i, base in enumerate((0.0, 3.99, 4.0)):
        run(cfg, [msg(1)], ai, sa_score(base), type(state)(tmp_path / f"s{i}"), log)
    assert len(ai.calls) == 2


def test_allowlisted_messages_are_never_sent(state, log):
    cfg = load(OJF_AI_URL=URL, **ACT_CFG)
    ai = FakeAi([])
    _, records, _ = run(cfg, [msg(1)], ai, sa_score(1.0, rules=["USER_IN_DEF_DKIM_WHITELIST"]), state, log)
    assert ai.calls == [] and records[0]["reason"] == "allowlisted" and records[0]["ai_score"] is None


def test_without_a_url_nothing_changes_even_if_an_ai_object_is_passed(state, log):
    cfg = load(**ACT_CFG)
    ai = FakeAi([AiResult(10.0, "x")])
    _, records, _ = run(cfg, [msg(1)], ai, sa_score(1.2), state, log)
    assert ai.calls == [] and records[0]["score"] == 1.2 and records[0]["ai_score"] is None


def test_the_scl_bonus_counts_before_the_range_is_checked(state, log):
    cfg = load(OJF_AI_URL=URL, **ACT_CFG)  # SA 3.5 + SCL 9 bonus 4.0 = 7.5, already above the delete threshold
    ai = FakeAi([])
    _, records, _ = run(cfg, [msg(1)], ai, sa_score(3.5, scl=9), state, log)
    assert ai.calls == [] and records[0]["decision"] == "deleted"


def test_an_unusable_reply_costs_only_that_message_but_a_dead_server_stops_the_run(state, log, caplog):
    cfg = load(OJF_AI_URL=URL, **ACT_CFG)
    ai = FakeAi([AiError("garbled"), AiError("connection refused", server_down=True)])
    messages = [msg(1), msg(2), msg(3), msg(4)]
    with caplog.at_level("INFO", logger="ojf-test"):
        _, records, _ = run(cfg, messages, ai, sa_score(1.2), state, log)
    assert len(ai.calls) == 2  # message 1 garbled (go on), message 2 server down (stop), messages 3 and 4 not asked
    assert [r["score"] for r in records] == [1.2, 1.2, 1.2, 1.2] and all(r["ai_score"] is None for r in records)
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "skipping AI scoring for the rest of this run" in text and "AI rated 0 of 4 scored messages" in text
    assert "2 failed" in text


def test_the_run_reports_how_many_messages_the_ai_rated(state, log, caplog):
    cfg = load(OJF_AI_URL=URL, **ACT_CFG)
    with caplog.at_level("INFO", logger="ojf-test"):
        run(cfg, [msg(1), msg(2)], FakeAi([AiResult(8.0, ""), AiResult(1.0, "")]), sa_score(1.0), state, log)
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "AI rated 2 of 2 scored messages (score range 0.0 to 6.0, up to +4.0 points); 0 failed" in text
