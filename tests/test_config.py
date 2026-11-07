import pytest

from outlook_junk_filter import config as config_module

CID = {"OJF_CLIENT_ID": "abc"}


def load(**env):
    return config_module.load({**CID, **env})


def test_client_id_is_required_and_empty_counts_as_unset():
    with pytest.raises(config_module.ConfigError):
        config_module.load({})
    with pytest.raises(config_module.ConfigError):
        config_module.load({"OJF_CLIENT_ID": "   "})


def test_defaults():
    cfg = load()
    assert cfg.interval == 900
    assert cfg.effective_mode("live") == cfg.effective_mode("outlook") == "report-only"
    assert cfg.delete_mode is None and cfg.delete_threshold is None and cfg.max_deletes is None
    assert cfg.keep_pattern.pattern == r"^USER_IN_.*(WHITE|WELCOME)LIST"
    assert cfg.version == "dev"
    assert cfg.problems == []


def test_interval_below_60_is_raised_and_logged():
    cfg = load(OJF_INTERVAL_SECONDS="10")
    assert cfg.interval == 60
    assert any("60" in p for p in cfg.problems)


def test_bad_interval_falls_back_to_default():
    cfg = load(OJF_INTERVAL_SECONDS="soon")
    assert cfg.interval == 900 and cfg.problems


def test_empty_values_count_as_unset():
    cfg = load(OJF_MODE_LIVE="", OJF_DELETE_MODE="", OJF_DELETE_THRESHOLD="", OJF_MAX_DELETES_PER_RUN="", OJF_INTERVAL_SECONDS="")
    assert cfg.interval == 900 and cfg.max_deletes is None
    assert cfg.problems == []


def test_act_needs_valid_delete_settings():
    ok = load(OJF_MODE_LIVE="act", OJF_DELETE_MODE="permanent", OJF_DELETE_THRESHOLD="12.5")
    assert ok.effective_mode("live") == "act" and ok.effective_mode("outlook") == "report-only"
    assert ok.delete_threshold == 12.5
    bad = load(OJF_MODE_LIVE="act", OJF_DELETE_MODE="permanent")
    assert bad.effective_mode("live") == "report-only"
    assert bad.run_problems("live") and not bad.run_problems("outlook")


def test_modes_are_case_insensitive_and_unknown_is_report_only():
    assert load(OJF_MODE_OUTLOOK="ACT", OJF_DELETE_MODE="deleted-items", OJF_DELETE_THRESHOLD="1").effective_mode("outlook") == "act"
    cfg = load(OJF_MODE_OUTLOOK="delete-everything")
    assert cfg.effective_mode("outlook") == "report-only" and cfg.problems


def test_invalid_cap_and_pattern_are_ignored_not_fatal():
    cfg = load(OJF_MAX_DELETES_PER_RUN="-3", OJF_KEEP_RULE_PATTERN="(")
    assert cfg.max_deletes is None
    assert cfg.keep_pattern.pattern == r"^USER_IN_.*(WHITE|WELCOME)LIST"
    assert len(cfg.problems) == 2


def test_describe_has_no_secrets_fields():
    text = str(load().describe()).lower()
    assert "token" not in text and "password" not in text
