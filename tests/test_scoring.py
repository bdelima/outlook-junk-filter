"""Parsing SpamAssassin's output (report_safe 0) and the subprocess wrapper, with SA mocked."""

import subprocess

import pytest

from outlook_junk_filter import scoring
from outlook_junk_filter.scoring import ScoreError

RAW = b"Authentication-Results: spf=pass\r\nSubject: hi\r\n\r\nbody"
OUT = (
    b"X-Spam-Status: Yes, score=12.4 required=5.0 tests=BAYES_00,\r\n"
    b"\tLOCAL_SUBJ_ALTCASE,URIBL_BLOCKED, USER_IN_DEF_DKIM_WL autolearn=no\r\n"
    b"\tautolearn_force=no version=4.0.0\r\n"
    b"X-Spam-RelaysUntrusted: [ ip=203.0.113.9 rdns=h.example.invalid helo=h by=mx.example.invalid ]\r\n"
    b"    [ ip=198.51.100.2 rdns=y ]\r\n"
    b"Subject: hi\r\n\r\nbody"
)


def test_parse_output_reads_score_rules_relays_and_auth_header():
    result = scoring.parse_output(OUT, RAW)
    assert result.score == 12.4
    assert result.rules == ["BAYES_00", "LOCAL_SUBJ_ALTCASE", "URIBL_BLOCKED", "USER_IN_DEF_DKIM_WL"]
    assert result.first_untrusted_ip == "203.0.113.9"
    assert "198.51.100.2" in result.relays_untrusted
    assert result.auth_results_present is True


def test_parse_output_handles_no_rules_and_negative_scores_and_missing_auth():
    out = b"X-Spam-Status: No, score=-1.9 required=5.0 tests=none autolearn=ham\r\n\r\nx"
    result = scoring.parse_output(out, b"Subject: s\r\n\r\nx")
    assert result.score == -1.9 and result.rules == []
    assert result.auth_results_present is False and result.first_untrusted_ip == ""


def test_parse_output_without_status_header_is_a_score_error():
    with pytest.raises(ScoreError):
        scoring.parse_output(b"Subject: x\r\n\r\nbody", RAW)


def test_score_runs_spamassassin_without_spamd_and_a_siteconfigpath(monkeypatch, tmp_path):
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(cmd=cmd, kw=kw)
        return subprocess.CompletedProcess(cmd, 0, stdout=OUT, stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = scoring.score(RAW, tmp_path)
    assert result.score == 12.4
    assert seen["cmd"][0] == "spamassassin" and "--siteconfigpath" in seen["cmd"] and "/config" in seen["cmd"]
    assert "spamc" not in seen["cmd"] and "spamd" not in " ".join(seen["cmd"])
    assert seen["kw"]["timeout"] == 120 and seen["kw"]["input"] == RAW
    assert seen["kw"]["env"]["HOME"].startswith(str(tmp_path))  # no home directory: HOME is under /state
    assert seen["kw"]["env"]["TMPDIR"].startswith(str(tmp_path))


def test_timeout_and_failure_are_score_errors(monkeypatch, tmp_path):
    def timeout(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 120)

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(ScoreError):
        scoring.score(RAW, tmp_path)

    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 255, b"", b"bad config"))
    with pytest.raises(ScoreError):
        scoring.score(RAW, tmp_path)


def test_lint_reports_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 255, b"", b"config: error"))
    ok, detail = scoring.lint(tmp_path)
    assert ok is False and "error" in detail
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, b"", b""))
    assert scoring.lint(tmp_path)[0] is True


def test_microsoft_scl_is_read_from_the_header_and_bad_values_are_ignored():
    def scl(header):
        raw = (b"X-MS-Exchange-Organization-SCL: " + header + b"\r\nSubject: x\r\n\r\nbody") if header is not None else b"Subject: x\r\n\r\nbody"
        return scoring.parse_output(OUT, raw).ms_scl

    assert scl(b"8") == 8 and scl(b" 9 ") == 9 and scl(b"-1") == -1
    assert scl(None) is None and scl(b"high") is None
    # the first (latest-added) header wins
    raw = b"X-MS-Exchange-Organization-SCL: 9\r\nX-MS-Exchange-Organization-SCL: 1\r\nSubject: x\r\n\r\nbody"
    assert scoring.parse_output(OUT, raw).ms_scl == 9
