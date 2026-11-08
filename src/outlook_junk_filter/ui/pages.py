"""HTML for the review page.

Every value that comes from the log is untrusted (senders and subjects are phishing mail), so
all of it goes through ``esc`` and nothing builds markup by concatenating a log field.
"""

from __future__ import annotations

import csv
import html
import io
from urllib.parse import urlencode

from . import actions as actions_module
from .logreader import PER_PAGE

CSV_COLUMNS = ("time", "sender", "subject", "score", "ms_scl", "decision", "reason")
DECISIONS = ("keep", "would-delete", "would-quarantine", "quarantined", "deleted", "permanently-deleted", "error")


def esc(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def link(path: str, params: dict, text: str) -> str:
    query = urlencode({k: v for k, v in params.items() if v not in ("", None)})
    return f'<a href="{esc(path + ("?" + query if query else ""))}">{esc(text)}</a>'


NAV = (("/", "Decisions"), ("/summary", "Summary"), ("/whatif", "What-if"))


APP_NAME = "SpamWatch"
ICON = (
    '<svg class="logo" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round" '
    'stroke-linecap="round" aria-hidden="true"><path d="M12 2.5l8 3v5.5c0 5-3.4 8.7-8 10-4.6-1.3-8-5-8-10V5.5z"/>'
    '<rect x="7.5" y="9" width="9" height="6.5" rx="1"/><path d="M7.5 10l4.5 3.2 4.5-3.2"/></svg>'
)
_brand = {"account": ""}


def _account() -> str:
    return esc(_brand["account"])


def set_account(address: str) -> None:
    """The mailbox this instance watches, shown under the name (one instance, one mailbox)."""
    _brand["account"] = str(address or "")


def layout(title: str, body: str, header: str = "", active: str = "") -> str:
    on = ' class="on"'
    tabs = "".join(f'<a href="{path}"{on if (active or "") == path else ""}>{label}</a>' for path, label in NAV)
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{esc(title)} · {APP_NAME}</title><link rel=\"icon\" type=\"image/svg+xml\" href=\"/static/icon.svg\">"
        "<link rel=\"stylesheet\" href=\"/static/style.css\"></head><body>"
        f"<nav><span class=\"brand\">{ICON}<span><span class=\"name\">{APP_NAME}</span>"
        f"<span class=\"mailbox\">{_account()}</span></span></span>{tabs}{header}</nav>"
        f"<main><h1>{esc(title)}</h1>{body}</main></body></html>"
    )


def header_block(info: dict) -> str:
    age = info.get("age_seconds")
    age_text = "no records yet" if age is None else _age(age)
    skipped = f" \u00b7 {esc(info['skipped'])} malformed line(s) skipped" if info.get("skipped") else ""
    return (
        '<span class="status">'
        f"{esc(info['records'])} records \u00b7 newest {esc(info['newest'] or '-')} \u00b7 last record {esc(age_text)}{skipped}"
        "</span>"
    )


def _age(seconds: int) -> str:
    if seconds < 0:
        return "in the future"
    if seconds < 90:
        return f"{seconds} s ago"
    if seconds < 5400:
        return f"{seconds // 60} min ago"
    if seconds < 172800:
        return f"{seconds // 3600} h ago"
    return f"{seconds // 86400} days ago"


def _score(value) -> str:
    return f"{value:.1f}" if isinstance(value, (int, float)) else "-"


COLUMNS = (
    # sort key, heading, default width in px
    ("time", "Time", 112),
    ("sender", "Sender", 190),
    ("subject", "Subject", 300),
    ("score", "Score", 70),
    ("scl", "SCL", 60),
    ("decision", "Decision", 120),
    (None, "Actions taken", 190),
)


def _time(ts) -> str:
    text = str(ts or "")
    return text[5:16].replace("T", " ") if len(text) >= 16 else text


def _chips(chips: list) -> str:
    if not chips:
        return '<span class="none">none</span>'
    return "".join(f'<span class="chip {esc(kind)}" title="{esc(ts)}">{esc(label)}</span>' for label, kind, ts in chips)


def _bubble(record: dict, explanation) -> str:
    """The hover text for a score: why it was decided, what fired, in plain words, and the AI's view."""
    parts = [f"<p><strong>{esc(str(record.get('decision') or '').capitalize())}:</strong> {esc(record.get('reason'))}</p>"]
    against = "; ".join(esc(p) for p in explanation.against)
    favor = "; ".join(esc(p) for p in explanation.favor)
    more = f"; +{esc(explanation.more)} minor finding(s)" if explanation.more else ""
    parts.append(f'<p><span class="k">Against it</span>{against + more if against else "nothing notable"}</p>')
    parts.append(f'<p><span class="k">In its favor</span>{favor or "nothing notable"}</p>')
    ai = record.get("ai_score")
    if isinstance(ai, (int, float)) and not isinstance(ai, bool):
        reason = str(record.get("ai_reason") or "")
        text = f"{ai:g} of 10" + (f": \u201c{reason[:200]}\u201d" if reason else "")
        parts.append(f'<p><span class="k">AI rating</span>{esc(text)}</p>')
    else:
        parts.append('<p><span class="k">AI rating</span><span class="none">not rated</span></p>')
    return '<div class="tip" role="tooltip">' + "".join(parts) + "</div>"


def _sort_head(key, heading, width, params):
    base = {k: v for k, v in params.items() if k not in ("page", "sort", "order")}
    if key is None:
        return f'<th data-w="{width}"><span class="h">{esc(heading)}</span><i class="grip"></i></th>'
    current = params.get("sort", "time") == key
    descending = params.get("order", "desc") == "desc"
    mark = (" \u25be" if descending else " \u25b4") if current else ""
    nxt = "asc" if (current and descending) else "desc"
    if key in ("sender", "subject", "decision") and not current:
        nxt = "asc"
    return (
        f'<th data-w="{width}"><a class="h" href="{esc("/?" + urlencode({**{k: v for k, v in base.items() if v}, "sort": key, "order": nxt}))}">'
        f"{esc(heading)}{mark}</a><i class=\"grip\"></i></th>"
    )


def _pager(page, params: dict, current: int, pages: int, base: dict) -> str:
    first = (current - 1) * PER_PAGE + 1 if page.total else 0
    last = min(page.total, current * PER_PAGE)
    out = f"{esc(first)}\u2013{esc(last)} of {esc(page.total)}"
    if current > 1:
        out += " \u00b7 " + link("/", {**base, "page": current - 1}, "Previous")
    if current < pages:
        out += " \u00b7 " + link("/", {**base, "page": current + 1}, "Next")
    return out + " \u00b7 " + link("/export.csv", {k: v for k, v in base.items() if k not in ("sort", "order")}, "Export CSV")


def decisions_page(page, params: dict, info: dict, token: str = "", can_act: bool = False, history=None, rules=None) -> str:
    form = (
        '<form method="get" action="/" class="filters">'
        f'<input type="search" name="q" class="grow" placeholder="Search sender or subject" value="{esc(params.get("q", ""))}">'
        f'<label>Decision <select name="decision">{_options(("",) + DECISIONS, params.get("decision", ""))}</select></label>'
        f'<label>Score <input name="score_min" size="4" placeholder="min" value="{esc(params.get("score_min", ""))}">'
        f' \u2013 <input name="score_max" size="4" placeholder="max" value="{esc(params.get("score_max", ""))}"></label>'
        f'<label>Dates <input type="date" name="date_from" value="{esc(params.get("date_from", ""))}">'
        f' \u2013 <input type="date" name="date_to" value="{esc(params.get("date_to", ""))}"></label>'
        f'<input type="hidden" name="sort" value="{esc(params.get("sort", "time"))}">'
        f'<input type="hidden" name="order" value="{esc(params.get("order", "desc"))}">'
        '<button type="submit" class="primary">Apply</button> <a class="btn" href="/">Reset</a></form>'
    )
    rows = []
    for record in page.rows:
        detail = {"id": record.get("message_id", ""), "ts": record.get("ts", "")}
        chips = history.chips(record) if history is not None else []
        explanation = rules.explain(record.get("rules")) if rules is not None else None
        score_text = _score(record.get("score"))
        score_cell = (
            f'<td class="num sc"><span class="score" tabindex="0">{esc(score_text)}</span>{_bubble(record, explanation)}</td>'
            if explanation is not None
            else f'<td class="num">{esc(score_text)}</td>'
        )
        rows.append(
            "<tr>"
            + (_checkbox(record) if can_act else "")
            + f'<td title="{esc(record.get("ts"))}">{link("/message", detail, _time(record.get("ts")))}</td>'
            f'<td title="{esc(record.get("from_addr"))}">{esc(record.get("from_addr"))}</td>'
            f'<td title="{esc(record.get("subject"))}">{esc(record.get("subject"))}</td>'
            + score_cell
            + f'<td class="num">{esc(record.get("ms_scl") if record.get("ms_scl") is not None else "-")}</td>'
            f"<td>{esc(record.get('decision'))}</td>"
            f'<td class="chips">{_chips(chips)}</td></tr>'
        )
    span = len(COLUMNS) + (1 if can_act else 0)
    cols = ("<col data-w=\"30\">" if can_act else "") + "".join(f'<col data-w="{w}">' for _, _, w in COLUMNS)
    heads = (
        '<th class="chk"><input type="checkbox" id="select-all" title="Select all on this page" aria-label="Select all on this page"></th>'
        if can_act
        else ""
    ) + "".join(_sort_head(key, heading, width, params) for key, heading, width in COLUMNS)
    table = (
        f'<div class="tablewrap"><table id="decisions"><colgroup>{cols}</colgroup><thead><tr>{heads}</tr></thead><tbody>'
        + ("".join(rows) or f'<tr><td colspan="{span}">No decisions to show.</td></tr>')
        + "</tbody></table></div>"
    )
    current = int(params.get("page", "1") or 1)
    total_pages = max(1, -(-page.total // PER_PAGE))
    base = {k: v for k, v in params.items() if k != "page"}
    pager = _pager(page, params, current, total_pages, base)
    skipped = f'<p class="meta">{esc(page.skipped)} malformed line(s) skipped.</p>' if page.skipped else ""
    if can_act:
        menu = "".join(
            f'<button type="submit" name="choose" value="{esc(action)}" class="item">{esc(text)} <span class="n" data-n="{esc(group)}"></span></button>'
            for action, text, group in MENU
        )
        bar = (
            '<div class="selbar"><span class="selinfo"><strong id="selcount">0 selected</strong>'
            f'<details class="actions"><summary class="btn">Actions \u25be</summary><div class="menu">{menu}</div></details>'
            '<button type="button" class="btn" id="clear-selection">Clear</button></span>'
            f'<span class="pager">{pager}</span></div>'
        )
        table = (
            '<form method="post" action="/batch">'
            f'<input type="hidden" name="token" value="{esc(token)}">' + bar + table + "</form>"
            f'<p class="pager below">{pager}</p><script src="/static/ui.js"></script>'
        )
    else:
        table = f'<div class="selbar"><span class="pager">{pager}</span></div>' + table + f'<p class="pager below">{pager}</p><script src="/static/ui.js"></script>'
    return layout("Decisions", form + table + skipped, header_block(info), "/")


MENU = (
    ("teach-spam", "Teach: spam", "teach"),
    ("teach-ham", "Teach: not spam", "teach"),
    ("restore-junk", "Restore to Junk", "restore"),
    ("restore-inbox", "Restore to Inbox", "restore"),
    ("allow-address", "Allowlist sender address", "allow"),
    ("allow-domain", "Allowlist sender domain", "allow"),
    ("delete-row", "Delete from log\u2026", "all"),
)


def _checkbox(record: dict) -> str:
    """A tick box for a row that a batch action could apply to; the value names the exact record."""
    flags = {
        "t": actions_module.eligible("teach-spam", record),
        "r": actions_module.eligible("restore-junk", record),
        "a": actions_module.eligible("allow-address", record),
    }
    if not any(flags.values()):
        return '<td class="chk"></td>'
    data = "".join(f' data-{k}="1"' for k, v in flags.items() if v)
    value = f"{record.get('ts', '')}|{record.get('message_id', '')}"
    return f'<td class="chk"><input type="checkbox" name="sel" value="{esc(value)}"{data} aria-label="Select this row"></td>'


def _options(values, selected) -> str:
    return "".join(
        f'<option value="{esc(v)}"{" selected" if v == selected else ""}>{esc(v or "any")}</option>' for v in values
    )


def message_page(record: dict, info: dict, history=None, rules=None) -> str:
    fields = [
        "ts", "message_id", "internet_message_id", "received", "from_addr", "subject", "score",
        "keep_rule_hit", "relays_untrusted", "first_untrusted_ip", "auth_results_present", "mode", "decision",
        "reason", "action_error", "ms_scl", "ai_score", "ai_reason", "current_message_id", "folder",
    ]
    items = "".join(f"<dt>{esc(name)}</dt><dd>{esc(record.get(name))}</dd>" for name in fields)
    names = record.get("rules") or []
    if names:
        lines = []
        for name in names:
            text = rules.detail(name) if rules is not None else ""
            shown = esc(text[:1].upper() + text[1:]) if text else '<span class="none">no description</span>'
            lines.append(f'<li>{shown} <code class="tok">{esc(name)}</code></li>')
        rule_list = "<ul>" + "".join(lines) + "</ul>"
    else:
        rule_list = "<p>none</p>"
    side = _actions_block(record, history)
    body = (
        f'<div class="detail"><div><dl>{items}</dl><h2>Rules that fired</h2>{rule_list}</div><aside>{side}</aside></div>'
        "<p>The log holds no message bodies. To read the message, open it in Outlook (in Junk or the quarantine folder).</p>"
    )
    return layout("Message", body, header_block(info), "/")


def _row(done: bool, label: str, when: str, links: str) -> str:
    mark = '<span class="ok">\u2713</span>' if done else '<span class="none">\u25cb</span>'
    stamp = f'<span class="none">{esc(when)}</span> ' if when else ""
    return f'<div class="arow"><span>{mark} {label}</span><span>{stamp}{links}</span></div>'


def _actions_block(record: dict, history=None) -> str:
    base = {"id": record.get("message_id", ""), "ts": record.get("ts", "")}

    def act(action, text):
        return link("/confirm", {**base, "action": action}, text)

    message_id = record.get("current_message_id") or ""
    out = ["<h2>Actions</h2>"]
    if not actions_module.can_teach(record):
        out.append("<p>This message is gone from Outlook (permanently deleted), so it cannot be taught or restored.</p>")
    else:
        taught = history.teach.get(message_id) if history is not None else None
        label = ("spam" if taught and taught[0] == "spam" else "not spam") if taught else ""
        out.append(
            _row(bool(taught), f"Taught Bayes: {esc(label)}" if taught else "Teach Bayes", taught[1] if taught else "",
                 act("teach-spam", "spam") + " \u00b7 " + act("teach-ham", "not spam"))
        )
    if actions_module.can_restore(record):
        restored = history.restore.get(message_id) if history is not None else None
        out.append(
            _row(bool(restored), f"Restored to {esc(restored[0].capitalize())}" if restored else "Restore", restored[1] if restored else "",
                 act("restore-junk", "to Junk") + " \u00b7 " + act("restore-inbox", "to Inbox"))
        )
    status = history.allow_status(record) if history is not None else {"address": None, "domain": None}
    address = str(record.get("from_addr") or "").strip().lower()
    if actions_module.eligible("allow-address", record):
        domain = address.split("@", 1)[1]
        out.append(_row(bool(status["address"]), "Sender address allowlisted" if status["address"] else "Allowlist sender address",
                        status["address"] or "", act("allow-address", "allowlist")))
        out.append(_row(bool(status["domain"]), f"Domain allowlisted ({esc(domain)})" if status["domain"] else "Allowlist sender domain",
                        status["domain"] or "", act("allow-domain", "allowlist")))
        out.append('<p class="meta">Allowlist entries count only when SPF or DKIM passes. A done action stays clickable to repeat it.</p>')
    else:
        out.append("<p>This sender address cannot be allowlisted (it is not a plain address).</p>")
    out.append(
        '<p class="danger-zone">' + act("delete-row", "Delete this log row\u2026") + "</p>"
    )
    return "".join(out)


ACTION_TEXT = {
    "teach-spam": "Teach Bayes that this message is spam",
    "teach-ham": "Teach Bayes that this message is not spam",
    "restore-junk": "Move this message back to Junk (the job will keep it)",
    "restore-inbox": "Move this message to the Inbox (the job will keep it)",
    "allow-address": "Allowlist this sender address",
    "allow-domain": "Allowlist this sender's whole domain",
    "delete-row": "Remove this row from the log (the email in Outlook is not touched; this can't be undone)",
}


def confirm_page(action: str, record: dict, token: str, info: dict) -> str:
    fields = {"id": record.get("message_id", ""), "ts": record.get("ts", ""), "action": action, "token": token}
    hidden = "".join(f'<input type="hidden" name="{esc(k)}" value="{esc(v)}">' for k, v in fields.items())
    body = (
        f"<p>{esc(ACTION_TEXT.get(action, action))}?</p>"
        f"<p>From {esc(record.get('from_addr'))}, subject {esc(record.get('subject'))}.</p>"
        f'<form method="post" action="/act">{hidden}<button type="submit">Confirm</button></form>'
        + link("/message", {"id": fields["id"], "ts": fields["ts"]}, "Cancel")
    )
    return layout("Confirm", body, header_block(info))


def result_page(message: str, ok: bool, back: str, info: dict) -> str:
    return layout("Done" if ok else "Not done", f"<p>{esc(message)}</p><p>{back}</p>", header_block(info))


def summary_page(data: dict, info: dict) -> str:
    def counts_table(title: str, rows: dict) -> str:
        cells = "".join(
            f"<tr><td>{esc(key)}</td><td>" + ", ".join(f"{esc(d)}: {esc(n)}" for d, n in sorted(v.items())) + "</td></tr>"
            for key, v in rows.items()
        )
        return f"<h2>{esc(title)}</h2><table><tbody>{cells or '<tr><td>nothing yet</td></tr>'}</tbody></table>"

    scl_rows = "".join(
        f"<tr><td>{esc(level)}</td><td class=\"num\">{esc(n)}</td><td class=\"num\">{esc(f'{avg:.1f}')}</td>"
        f"<td class=\"num\">{esc(f'{top:.1f}')}</td></tr>"
        for level, (n, avg, top) in data.get("per_scl", {}).items()
    )
    hist = "".join(f"<tr><td>{esc(b)}</td><td>{esc(n)}</td></tr>" for b, n in data["histogram"].items())
    body = (
        counts_table("Per day", data["per_day"])
        + "<h2>Microsoft SCL (latest record per message)</h2><table><thead><tr><th>SCL</th><th>Messages</th>"
        "<th>Average score</th><th>Highest score</th></tr></thead>"
        f"<tbody>{scl_rows or '<tr><td colspan=4>nothing yet</td></tr>'}</tbody></table>"
        + "<h2>Score histogram</h2><table><thead><tr><th>Score (whole-number bucket)</th><th>Messages</th></tr></thead>"
        f"<tbody>{hist or '<tr><td colspan=2>nothing yet</td></tr>'}</tbody></table>"
    )
    return layout("Summary", body, header_block(info))


def whatif_page(params: dict, result: dict | None, error: str, info: dict) -> str:
    form = (
        '<form method="get" action="/whatif">'
        f'<label>Quarantine threshold <input name="quarantine" size="6" value="{esc(params.get("quarantine", ""))}"></label> '
        f'<label>Delete threshold <input name="delete" size="6" value="{esc(params.get("delete", ""))}"></label> '
        '<button type="submit">Count</button></form>'
        "<p>Counts use the scores the messages had when they were scored, so they do not reflect Bayes learning since. "
        "Nothing is saved.</p>"
    )
    out = ""
    if error:
        out = f'<p class="error">{esc(error)}</p>'
    elif result is not None:
        out = (
            f"<table><tbody><tr><td>Logged messages</td><td>{esc(result['total'])}</td></tr>"
            f"<tr><td>Would be kept</td><td>{esc(result['kept'])} (of which allowlisted: {esc(result['allowlisted'])})</td></tr>"
            f"<tr><td>Would be quarantined</td><td>{esc(result['quarantined'])}</td></tr>"
            f"<tr><td>Would be deleted</td><td>{esc(result['deleted'])}</td></tr></tbody></table>"
        )
    return layout("What-if", form + out, header_block(info))


def csv_cell(value) -> str:
    """A cell that starts with =, +, - or @ is prefixed so a spreadsheet does not run it as a formula."""
    text = "" if value is None else str(value)
    if text and text[0] in "=+-@\t\r":
        return "'" + text
    return text


def csv_rows(records):
    """Yield the CSV text, one chunk per row: the header, then the columns of the Decisions view."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(CSV_COLUMNS)
    yield buffer.getvalue()
    for record in records:
        buffer.seek(0)
        buffer.truncate()
        writer.writerow(
            [
                csv_cell(record.get("ts")),
                csv_cell(record.get("from_addr")),
                csv_cell(record.get("subject")),
                csv_cell(_score(record.get("score"))),
                csv_cell("" if record.get("ms_scl") is None else record.get("ms_scl")),
                csv_cell(record.get("decision")),
                csv_cell(record.get("reason")),
            ]
        )
        yield buffer.getvalue()


BATCH_TEXT = {
    "teach-spam": "Teach Bayes: spam",
    "teach-ham": "Teach Bayes: not spam",
    "restore-junk": "Restore to Junk",
    "restore-inbox": "Restore to Inbox",
    "allow-address": "Allowlist sender address",
    "allow-domain": "Allowlist sender domain",
    "delete-row": "Delete from log",
}


def batch_page(records: list, missing: int, keys: list, token: str, info: dict, chosen: str = "") -> str:
    """Show the selected rows and the actions that apply to at least one of them."""
    hidden = f'<input type="hidden" name="token" value="{esc(token)}">' + "".join(
        f'<input type="hidden" name="sel" value="{esc(k)}">' for k in keys
    )
    buttons = []
    for action, text in BATCH_TEXT.items():
        count = sum(1 for r in records if actions_module.eligible(action, r))
        if count:
            css = (' class="danger"' if action == "delete-row" else ' class="primary"') if action == chosen else (
                ' class="danger-text"' if action == "delete-row" else ""
            )
            buttons.append(
                f'<button type="submit" name="action" value="{esc(action)}"{css}>{esc(text)} ({count} of {len(records)})</button>'
            )
    rows = "".join(
        "<tr>"
        f"<td>{esc(r.get('ts'))}</td><td>{esc(r.get('from_addr'))}</td><td>{esc(r.get('subject'))}</td>"
        f"<td>{esc(r.get('decision'))}</td><td>{esc(r.get('folder') or '-')}</td></tr>"
        for r in records
    )
    note = f'<p class="error">{esc(missing)} selected row(s) are no longer in the log and are left out.</p>' if missing else ""
    lead = ""
    if chosen == "delete-row":
        lead = (
            f"<p><strong>Remove {esc(len(records))} row(s) from the log?</strong> Only the history rows on this page. "
            "The emails in Outlook and what the job has already decided are not touched. This can't be undone.</p>"
        )
    elif chosen in BATCH_TEXT:
        count = sum(1 for r in records if actions_module.eligible(chosen, r))
        lead = (
            f"<p><strong>{esc(BATCH_TEXT[chosen])}</strong> applies to {esc(count)} of {esc(len(records))} selected message(s). "
            "Press its button to run it; the others are listed in case you picked the wrong one.</p>"
            if count
            else f"<p><strong>{esc(BATCH_TEXT[chosen])}</strong> does not apply to any of the selected messages.</p>"
        )
    if buttons:
        actions = (
            f'<form method="post" action="/batch">{hidden}'
            f'<p class="buttons">{" ".join(buttons)}</p></form>'
            '<p class="meta">Each button applies only to the rows it fits; the count shows how many. '
            "Teaching runs one message at a time, so a large selection takes a while.</p>"
        )
    else:
        actions = "<p>None of the selected messages can be taught, restored or allowlisted.</p>"
    body = (
        f"<p>{esc(len(records))} message(s) selected.</p>"
        + lead
        + note
        + actions
        + '<div class="tablewrap"><table><thead><tr><th>Time</th><th>Sender</th><th>Subject</th><th>Decision</th><th>Folder</th></tr></thead>'
        f"<tbody>{rows}</tbody></table></div><p>{link('/', {}, 'Back to the decisions')}</p>"
    )
    return layout("Batch actions", body, header_block(info), "/")


def batch_result_page(action: str, results: list, missing: int, info: dict) -> str:
    done = sum(1 for _, ok, _ in results if ok)
    problems = [(r, m) for r, ok, m in results if not ok]
    rows = "".join(
        f"<tr><td>{esc(r.get('from_addr'))}</td><td>{esc(r.get('subject'))}</td><td>{esc(m)}</td></tr>" for r, m in problems
    )
    body = (
        f"<p>{esc(BATCH_TEXT.get(action, action))}: {esc(done)} done, {esc(len(problems))} not done"
        + (f", {esc(missing)} no longer in the log" if missing else "")
        + ".</p>"
        + (
            "<h2>Not done</h2><table><thead><tr><th>Sender</th><th>Subject</th><th>Why</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>"
            if problems
            else ""
        )
        + f"<p>{link('/', {}, 'Back to the decisions')}</p>"
    )
    return layout("Batch result", body, header_block(info))
