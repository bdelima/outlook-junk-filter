"""Plain-English descriptions of the SpamAssassin rules a message hit.

Three sources, in order: a hand-written phrase for the common rules (``PHRASES``), then SpamAssassin's own
one-line ``describe`` text read from the rule files in the image, then the raw rule name. The stock scores in the
same files order the phrases (the log stores rule names, not their points). Everything is read-only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

DEFAULT_DIRS = ("/usr/share/spamassassin", "/var/lib/spamassassin", "/usr/local/share")
MAX_BAD = 5
MAX_GOOD = 3

BAD, GOOD, QUIET = "bad", "good", "quiet"

# kind, fragment. A fragment finishes the sentence "It ..." loosely and reads on its own after a semicolon.
PHRASES = {
    # sender checks
    "SPF_FAIL": (BAD, "the sending server isn't allowed to send for that domain (SPF fail)"),
    "SPF_SOFTFAIL": (BAD, "the sending server probably isn't allowed to send for that domain (SPF soft fail)"),
    "SPF_HELO_FAIL": (BAD, "the sending server's own name fails its SPF check"),
    "SPF_PASS": (GOOD, "the sending server is allowed to send for the domain (SPF pass)"),
    "SPF_HELO_PASS": (QUIET, ""),
    "SPF_NONE": (QUIET, ""),
    "SPF_HELO_NONE": (QUIET, ""),
    "DKIM_INVALID": (BAD, "the sender's DKIM signature doesn't check out"),
    "DKIM_VALID": (GOOD, "the sender's DKIM signature is valid"),
    "DKIM_VALID_AU": (GOOD, "the DKIM signature is valid and matches the sender's address"),
    "DKIM_VALID_EF": (GOOD, "the DKIM signature is valid and matches the envelope sender"),
    "DKIM_SIGNED": (QUIET, ""),
    "DMARC_REJECT": (BAD, "the sender's domain asks that mail failing its checks be rejected, and this one fails"),
    "DMARC_QUAR": (BAD, "the sender's domain asks that mail failing its checks be quarantined, and this one fails"),
    "DMARC_PASS": (GOOD, "it passes the sender's DMARC policy"),
    "DMARC_MISSING": (QUIET, ""),
    "ENV_AND_HDR_SPF_MATCH": (GOOD, "the From address matches the SPF-checked sender"),
    "HEADER_FROM_DIFFERENT_DOMAINS": (BAD, "the From address and the real sender are on different domains"),
    "FREEMAIL_FROM": (QUIET, ""),
    "FREEMAIL_REPLYTO": (BAD, "replies go to a free email address"),
    "FREEMAIL_FORGED_REPLYTO": (BAD, "replies go to a free email address that doesn't match the sender"),
    "RDNS_NONE": (BAD, "the sending server has no reverse-DNS name"),
    "RDNS_DYNAMIC": (BAD, "it was sent from a home or dynamic internet address"),
    "FORGED_MUA_OUTLOOK": (BAD, "it claims to come from Outlook but doesn't look like it"),
    "MISSING_MID": (BAD, "it has no message ID"),
    "MISSING_DATE": (BAD, "it has no date"),
    "MISSING_HEADERS": (BAD, "it is missing standard headers"),
    "INVALID_MSGID": (BAD, "its message ID is malformed"),
    "FROM_LOCAL_DIGITS": (BAD, "the sender's address is full of digits"),
    "FROM_LOCAL_HEX": (BAD, "the sender's address looks randomly generated"),
    "USER_IN_WHITELIST": (GOOD, "the sender is on your allowlist"),
    "USER_IN_DEF_WHITELIST": (GOOD, "the sender is on the built-in allowlist"),
    "USER_IN_WELCOMELIST": (GOOD, "the sender is on your allowlist"),
    "USER_IN_DEF_WELCOMELIST": (GOOD, "the sender is on the built-in allowlist"),
    # blocklists
    "URIBL_BLACK": (BAD, "it links to a domain on a spam blocklist"),
    "URIBL_DBL_SPAM": (BAD, "it links to a domain on the Spamhaus spam list"),
    "URIBL_DBL_PHISH": (BAD, "it links to a domain on the Spamhaus phishing list"),
    "URIBL_DBL_MALWARE": (BAD, "it links to a domain on the Spamhaus malware list"),
    "URIBL_SBL": (BAD, "it links to an address on the Spamhaus blocklist"),
    "URIBL_SBL_A": (BAD, "it links to a site hosted on the Spamhaus blocklist"),
    "URIBL_ABUSE_SURBL": (BAD, "it links to a domain on the SURBL abuse list"),
    "URIBL_PH_SURBL": (BAD, "it links to a domain on the SURBL phishing list"),
    "URIBL_MW_SURBL": (BAD, "it links to a domain on the SURBL malware list"),
    "URIBL_BLOCKED": (QUIET, ""),
    "URIBL_GREY": (BAD, "it links to a domain on a suspect list"),
    "RCVD_IN_BL_SPAMCOP_NET": (BAD, "it came through a server on the SpamCop blocklist"),
    "RCVD_IN_XBL": (BAD, "it came from an address on the Spamhaus exploited-machines list"),
    "RCVD_IN_SBL": (BAD, "it came from an address on the Spamhaus blocklist"),
    "RCVD_IN_PBL": (BAD, "it came from a home or dynamic address on the Spamhaus list"),
    "RCVD_IN_VALIDITY_RPBL": (BAD, "it came from an address on the Validity blocklist"),
    "RCVD_IN_VALIDITY_RPBL_BLOCKED": (QUIET, ""),
    "RCVD_IN_VALIDITY_SAFE": (GOOD, "it came from a server Validity vouches for"),
    "RCVD_IN_VALIDITY_CERTIFIED": (GOOD, "it came from a server Validity certifies"),
    "RCVD_IN_DNSWL_NONE": (QUIET, ""),
    "RCVD_IN_DNSWL_LOW": (GOOD, "it came from a server on a trusted-sender list"),
    "RCVD_IN_DNSWL_MED": (GOOD, "it came from a server on a trusted-sender list"),
    "RCVD_IN_DNSWL_HI": (GOOD, "it came from a server on a trusted-sender list"),
    "RCVD_IN_MSPIKE_H2": (GOOD, "it came from a server with a good reputation"),
    "RCVD_IN_MSPIKE_BL": (BAD, "it came from a server with a poor reputation"),
    "RCVD_IN_MSPIKE_L2": (BAD, "it came from a server with a poor reputation"),
    "RCVD_IN_MSPIKE_L3": (BAD, "it came from a server with a poor reputation"),
    "RCVD_IN_MSPIKE_L4": (BAD, "it came from a server with a poor reputation"),
    "RCVD_IN_MSPIKE_L5": (BAD, "it came from a server with a very poor reputation"),
    "RCVD_IN_MSPIKE_ZBI": (QUIET, ""),
    "RCVD_IN_ZEN": (BAD, "it came from an address on the Spamhaus combined blocklist"),
    # learned filter
    "BAYES_99": (BAD, "the learned filter is almost certain it is spam (99%+)"),
    "BAYES_95": (BAD, "the learned filter thinks it is very likely spam (95%+)"),
    "BAYES_80": (BAD, "the learned filter thinks it is likely spam (80%+)"),
    "BAYES_60": (BAD, "the learned filter leans toward spam (60%+)"),
    "BAYES_50": (QUIET, ""),
    "BAYES_40": (QUIET, ""),
    "BAYES_20": (GOOD, "the learned filter leans toward not spam"),
    "BAYES_05": (GOOD, "the learned filter thinks it is likely not spam"),
    "BAYES_00": (GOOD, "the learned filter is nearly certain it is not spam"),
    # content
    "MIME_HTML_ONLY": (BAD, "it has an HTML body and no plain-text version"),
    "HTML_IMAGE_ONLY_04": (BAD, "it is almost all one image with hardly any text"),
    "HTML_IMAGE_ONLY_08": (BAD, "it is almost all one image with hardly any text"),
    "HTML_IMAGE_ONLY_12": (BAD, "it is almost all one image with hardly any text"),
    "HTML_IMAGE_ONLY_16": (BAD, "it is almost all one image with hardly any text"),
    "HTML_IMAGE_ONLY_20": (BAD, "it is almost all one image with hardly any text"),
    "HTML_IMAGE_ONLY_24": (BAD, "it is almost all one image with hardly any text"),
    "HTML_IMAGE_ONLY_28": (BAD, "it is almost all one image with hardly any text"),
    "HTML_IMAGE_ONLY_32": (BAD, "it is almost all one image with hardly any text"),
    "HTML_FONT_SIZE_LARGE": (BAD, "it uses very large text"),
    "HTML_FONT_SIZE_HUGE": (BAD, "it uses huge text"),
    "HTML_TITLE_SUBJ_DIFF": (BAD, "the hidden page title differs from the subject"),
    "HTML_EXTRA_CLOSE": (BAD, "its HTML is malformed"),
    "HTML_MIME_NO_HTML_TAG": (BAD, "it claims to be HTML but has no HTML tags"),
    "HTML_MESSAGE": (QUIET, ""),
    "MPART_ALT_DIFF": (QUIET, ""),
    "MIME_QP_LONG_LINE": (QUIET, ""),
    "SUBJ_ALL_CAPS": (BAD, "the subject is in all capitals"),
    "SUBJ_EXCESS_PUNCT": (BAD, "the subject has excess punctuation"),
    "DEAR_SOMETHING": (BAD, "it opens with a generic greeting"),
    "LOTS_OF_MONEY": (BAD, "it mentions large sums of money"),
    "MILLION_USD": (BAD, "it mentions millions of dollars"),
    "MONEY_FORM": (BAD, "it asks about handling money"),
    "ADVANCE_FEE_2": (BAD, "it reads like an advance-fee scam"),
    "ADVANCE_FEE_3": (BAD, "it reads like an advance-fee scam"),
    "ADVANCE_FEE_4": (BAD, "it reads like an advance-fee scam"),
    "ADVANCE_FEE_5": (BAD, "it reads like an advance-fee scam"),
    "DRUGS_ERECTILE": (BAD, "it pitches an erectile-dysfunction drug"),
    "DRUGS_ERECTILE_OBFU": (BAD, "it pitches an erectile-dysfunction drug using disguised spellings"),
    "DRUGS_MANYKINDS": (BAD, "it lists many kinds of drugs"),
    "DRUGS_MUSCLE": (BAD, "it pitches muscle-building drugs"),
    "DRUGS_DIET": (BAD, "it pitches diet drugs"),
    "DRUGS_DIET_OBFU": (BAD, "it pitches diet drugs using disguised spellings"),
    "T_REMOTE_IMAGE": (QUIET, ""),
    "TRACKER_ID": (BAD, "it carries a tracking ID"),
    "URI_HEX": (BAD, "it links to an address written in hex"),
    "URI_NOVOWEL": (BAD, "it links to a domain with no vowels"),
    "URI_TRUNCATED": (BAD, "it contains a cut-off link"),
    "NORMAL_HTTP_TO_IP": (BAD, "it links straight to an IP address"),
    "NO_RELAYS": (QUIET, ""),
    "NO_RECEIVED": (QUIET, ""),
    "UNPARSEABLE_RELAY": (QUIET, ""),
    "T_SCC_BODY_TEXT_LINE": (QUIET, ""),
    # this project's own rules
    "OJF_ED_SPAM": (BAD, "it pitches erectile-dysfunction treatment without naming a drug"),
    "OJF_WEIGHTLOSS_AD": (BAD, "it looks like a weight-loss or GLP-1 ad that Microsoft also flagged"),
    "OJF_JUNK_ADMITS_SPAM": (BAD, "Microsoft filed it as junk and it calls itself an advertisement"),
}

_SCL = re.compile(r"^OJF_MS_SCL_(\d+)$")
_AI = re.compile(r"^OJF_AI_(\d+)$")
_DESCRIBE = re.compile(r"^describe\s+(\S+)\s+(.+?)\s*$")
_SCORE = re.compile(r"^score\s+(\S+)\s+(-?\d+(?:\.\d+)?)((?:\s+-?\d+(?:\.\d+)?){0,3})\s*(?:#.*)?$")


@dataclass
class Explanation:
    against: list = field(default_factory=list)  # phrases, most significant first
    favor: list = field(default_factory=list)
    more: int = 0  # findings left out of the lists above


class Rules:
    """SpamAssassin's descriptions and stock scores, read once from the rule files."""

    def __init__(self, dirs=DEFAULT_DIRS):
        self.descriptions: dict = {}
        self.scores: dict = {}
        for directory in dirs:
            root = Path(directory)
            if not root.is_dir():
                continue
            for path in sorted(root.rglob("*.cf")):
                self._read(path)

    def _read(self, path: Path) -> None:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return
        for line in lines:
            match = _DESCRIBE.match(line)
            if match:
                self.descriptions[match.group(1)] = match.group(2)
                continue
            match = _SCORE.match(line)
            if match:
                values = [float(match.group(2))] + [float(v) for v in match.group(3).split()]
                # four values are the score sets for (no Bayes/no net, no Bayes/net, Bayes/no net, Bayes/net)
                self.scores[match.group(1)] = values[3] if len(values) == 4 else values[0]

    def describe(self, rule: str) -> str:
        """One phrase for a rule, or an empty string when it is not worth showing."""
        match = _SCL.match(rule)
        if match:
            level = int(match.group(1))
            if level >= 7:
                return f"Microsoft rated it highly likely spam (SCL {level})"
            return f"Microsoft rated it likely spam (SCL {level})"
        match = _AI.match(rule)
        if match:
            return f"the AI model rated it {match.group(1)} out of 10"
        known = PHRASES.get(rule)
        if known:
            return known[1]
        text = self.descriptions.get(rule, "")
        return text.rstrip(".") if text else ""

    def detail(self, rule: str) -> str:
        """Like describe, but for the details page: never empty, so a quiet rule still gets a line."""
        text = self.describe(rule)
        if text:
            return text
        own = self.descriptions.get(rule, "").rstrip(".")
        return own or "informational; it has no effect on the verdict"

    def kind(self, rule: str) -> str:
        if _SCL.match(rule) or _AI.match(rule):
            return BAD
        known = PHRASES.get(rule)
        if known:
            return known[0]
        score = self.scores.get(rule)
        if score is None or score == 0:
            return QUIET
        return BAD if score > 0 else GOOD

    def explain(self, rules) -> Explanation:
        bad, good, seen = [], [], set()
        for rule in rules or []:
            kind = self.kind(rule)
            phrase = self.describe(rule)
            if kind == QUIET or not phrase or phrase in seen:
                continue
            seen.add(phrase)
            weight = abs(self.scores.get(rule, 1.0 if rule in PHRASES else 0.5))
            if _SCL.match(rule) or _AI.match(rule):
                weight = 100.0  # the extra evidence this project adds is always worth showing first
            (bad if kind == BAD else good).append((weight, phrase))
        bad.sort(key=lambda item: -item[0])
        good.sort(key=lambda item: -item[0])
        more = max(0, len(bad) - MAX_BAD) + max(0, len(good) - MAX_GOOD)
        return Explanation([p for _, p in bad[:MAX_BAD]], [p for _, p in good[:MAX_GOOD]], more)


@lru_cache(maxsize=1)
def default_rules() -> Rules:
    return Rules()
