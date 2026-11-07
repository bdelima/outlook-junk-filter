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
    assert cfg.delete_mode is None and cfg.delete_threshold is None and cfg.quarantine_threshold is None
    assert cfg.quarantine_enabled is True and cfg.quarantine_folder == "Quarantined" and cfg.quarantine_retention_days == 7
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
    cfg = load(OJF_MODE_LIVE="", OJF_DELETE_MODE="", OJF_DELETE_THRESHOLD="", OJF_QUARANTINE_THRESHOLD="", OJF_QUARANTINE_FOLDER="", OJF_INTERVAL_SECONDS="")
    assert cfg.interval == 900 and cfg.quarantine_folder == "Quarantined"
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


def test_invalid_pattern_is_ignored_not_fatal():
    cfg = load(OJF_KEEP_RULE_PATTERN="(")
    assert cfg.keep_pattern.pattern == r"^USER_IN_.*(WHITE|WELCOME)LIST"
    assert len(cfg.problems) == 1


def test_there_is_no_deletion_cap_setting():
    cfg = load(OJF_MAX_DELETES_PER_RUN="5")
    assert not hasattr(cfg, "max_deletes")
    assert "max_deletes_per_run" not in cfg.describe()


def test_describe_has_no_secrets_fields():
    text = str(load().describe()).lower()
    assert "token" not in text and "password" not in text


ACT = {"OJF_MODE_LIVE": "act", "OJF_DELETE_MODE": "deleted-items"}


def test_act_with_only_a_quarantine_threshold_is_valid():
    cfg = load(**ACT, OJF_QUARANTINE_THRESHOLD="6")
    assert cfg.effective_mode("live") == "act" and cfg.delete_threshold is None and cfg.quarantine_active


def test_act_needs_some_threshold_and_a_delete_mode():
    assert load(**ACT).effective_mode("live") == "report-only"  # neither threshold
    assert load(OJF_MODE_LIVE="act", OJF_QUARANTINE_THRESHOLD="6").effective_mode("live") == "report-only"  # no delete mode
    # a quarantine threshold counts only while quarantine is on
    off = load(**ACT, OJF_QUARANTINE_THRESHOLD="6", OJF_QUARANTINE_ENABLED="off")
    assert off.effective_mode("live") == "report-only" and not off.quarantine_active


def test_quarantine_threshold_must_be_lower_than_the_delete_threshold():
    for q, d in (("10", "10"), ("12", "10")):
        cfg = load(**ACT, OJF_QUARANTINE_THRESHOLD=q, OJF_DELETE_THRESHOLD=d)
        assert cfg.effective_mode("live") == "report-only"
        assert any("lower than" in p for p in cfg.run_problems("live"))
    ok = load(**ACT, OJF_QUARANTINE_THRESHOLD="6", OJF_DELETE_THRESHOLD="10")
    assert ok.effective_mode("live") == "act"
    # when quarantine is off the two thresholds are not compared
    off = load(**ACT, OJF_QUARANTINE_THRESHOLD="12", OJF_DELETE_THRESHOLD="10", OJF_QUARANTINE_ENABLED="off")
    assert off.effective_mode("live") == "act"


def test_quarantine_settings():
    cfg = load(OJF_QUARANTINE_FOLDER=" Suspect ", OJF_QUARANTINE_RETENTION_DAYS="30", OJF_QUARANTINE_ENABLED="false")
    assert cfg.quarantine_folder == "Suspect" and cfg.quarantine_retention_days == 30 and not cfg.quarantine_enabled
    bad = load(OJF_QUARANTINE_RETENTION_DAYS="soon", OJF_QUARANTINE_ENABLED="maybe")
    assert bad.quarantine_retention_days == 7 and bad.quarantine_enabled and len(bad.problems) == 2
