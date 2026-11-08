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


def layout(title: str, body: str, header: str = "") -> str:
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{esc(title)}</title><link rel=\"stylesheet\" href=\"/static/style.css\"></head><body>"
        "<nav><strong>outlook-junk-filter</strong> "
        '<a href="/">Decisions</a> <a href="/summary">Summary</a> <a href="/whatif">What-if</a></nav>'
        f"{header}<main><h1>{esc(title)}</h1>{body}</main></body></html>"
    )


def header_block(info: dict) -> str:
    age = info.get("age_seconds")
    age_text = "no records yet" if age is None else _age(age)
    skipped = f", {esc(info['skipped'])} malformed line(s) skipped" if info.get("skipped") else ""
    return (
        '<p class="meta">'
        f"{esc(info['records'])} records loaded; oldest {esc(info['oldest'] or '-')}, newest {esc(info['newest'] or '-')}; "
        f"last record {esc(age_text)}{skipped}. New records appear on reload.</p>"
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


def decisions_page(page, params: dict, info: dict, token: str = "", can_act: bool = False) -> str:
    form = (
        '<form method="get" action="/" class="filters">'
        f'<label>Decision <select name="decision">{_options(("",) + DECISIONS, params.get("decision", ""))}</select></label> '
        f'<label>From <input type="date" name="date_from" value="{esc(params.get("date_from", ""))}"></label> '
        f'<label>To <input type="date" name="date_to" value="{esc(params.get("date_to", ""))}"></label> '
        f'<label>Score min <input name="score_min" size="5" value="{esc(params.get("score_min", ""))}"></label> '
        f'<label>max <input name="score_max" size="5" value="{esc(params.get("score_max", ""))}"></label> '
        f'<label>Search <input name="q" value="{esc(params.get("q", ""))}"></label> '
        f'<label>Sort <select name="sort">{_options(("time", "score"), params.get("sort", "time"))}</select></label> '
        f'<label><select name="order">{_options(("desc", "asc"), params.get("order", "desc"))}</select></label> '
        '<button type="submit">Apply</button></form>'
    )
    rows = []
    for record in page.rows:
        detail = {"id": record.get("message_id", ""), "ts": record.get("ts", "")}
        rows.append(
            "<tr>"
            + (_checkbox(record) if can_act else "")
            + f"<td>{link('/message', detail, record.get('ts', ''))}</td>"
            f"<td>{esc(record.get('from_addr'))}</td>"
            f"<td>{esc(record.get('subject'))}</td>"
            f"<td class=\"num\">{esc(_score(record.get('score')))}</td>"
            f"<td class=\"num\">{esc(record.get('ms_scl') if record.get('ms_scl') is not None else '-')}</td>"
            f"<td>{esc(record.get('decision'))}</td>"
            f"<td>{esc(record.get('reason'))}</td></tr>"
        )
    table = (
        "<table><thead><tr>"
        + ('<th><input type="checkbox" id="select-all" title="Select all on this page"></th>' if can_act else "")
        + "<th>Time</th><th>Sender</th><th>Subject</th><th>Score</th><th>MS SCL</th>"
        "<th>Decision</th><th>Reason</th></tr></thead><tbody>"
        + ("".join(rows) or f'<tr><td colspan="{8 if can_act else 7}">No decisions to show.</td></tr>')
        + "</tbody></table>"
    )
    if can_act:
        table = (
            '<form method="post" action="/batch">'
            f'<input type="hidden" name="token" value="{esc(token)}">'
            '<p><button type="submit">Batch actions for the selected&hellip;</button> '
            '<span class="meta">Tick rows, then choose what to do with them.</span></p>'
            + table
            + "</form>"
            '<script src="/static/batch.js"></script>'
        )
    current = int(params.get("page", "1") or 1)
    pages = max(1, -(-page.total // PER_PAGE))
    base = {k: v for k, v in params.items() if k != "page"}
    pager = "<p class=\"pager\">"
    if current > 1:
        pager += link("/", {**base, "page": current - 1}, "Previous") + " "
    pager += f"Page {esc(current)} of {esc(pages)} ({esc(page.total)} matching) "
    if current < pages:
        pager += link("/", {**base, "page": current + 1}, "Next") + " "
    pager += link("/export.csv", base, "Export CSV") + "</p>"
    skipped = f'<p class="meta">{esc(page.skipped)} malformed line(s) skipped.</p>' if page.skipped else ""
    return layout("Decisions", form + pager + table + pager + skipped, header_block(info))


def _checkbox(record: dict) -> str:
    """A tick box for a row that a batch action could apply to; the value names the exact record."""
    if not (actions_module.eligible("teach-spam", record) or actions_module.eligible("restore-junk", record)):
        return "<td></td>"
    value = f"{record.get('ts', '')}|{record.get('message_id', '')}"
    return f'<td><input type="checkbox" name="sel" value="{esc(value)}"></td>'


def _options(values, selected) -> str:
    return "".join(
        f'<option value="{esc(v)}"{" selected" if v == selected else ""}>{esc(v or "any")}</option>' for v in values
    )


def message_page(record: dict, info: dict, quarantine_folder: str = "") -> str:
    fields = [
        "ts", "message_id", "internet_message_id", "received", "from_addr", "subject", "score",
        "keep_rule_hit", "relays_untrusted", "first_untrusted_ip", "auth_results_present", "mode", "decision",
        "reason", "action_error", "ms_scl", "ai_score", "ai_reason", "current_message_id", "folder",
    ]
    items = "".join(f"<dt>{esc(name)}</dt><dd>{esc(record.get(name))}</dd>" for name in fields)
    rules = record.get("rules") or []
    rule_list = "<ul>" + "".join(f"<li>{esc(rule)}</li>" for rule in rules) + "</ul>" if rules else "<p>none</p>"
    body = f"<dl>{items}</dl><h2>Rules that fired</h2>{rule_list}" + _actions_block(record)
    body += "<p>The log holds no message bodies. To read the message, open it in Outlook (in Junk or the quarantine folder).</p>"
    return layout("Message", body, header_block(info))


def _actions_block(record: dict) -> str:
    base = {"id": record.get("message_id", ""), "ts": record.get("ts", "")}
    out = ["<h2>Actions</h2>"]
    if not actions_module.can_teach(record):
        out.append("<p>This message is gone from Outlook (permanently deleted), so this row is view-only for teaching and restoring.</p>")
    else:
        out.append(
            "<p>Teach Bayes: "
            + link("/confirm", {**base, "action": "teach-spam"}, "this is spam")
            + " &middot; "
            + link("/confirm", {**base, "action": "teach-ham"}, "this is not spam")
            + "</p>"
        )
    if actions_module.can_restore(record):
        out.append(
            "<p>Restore: "
            + link("/confirm", {**base, "action": "restore-junk"}, "back to Junk")
            + " &middot; "
            + link("/confirm", {**base, "action": "restore-inbox"}, "to the Inbox")
            + "</p>"
        )
    out.append(
        "<p>Allowlist (counts only when SPF or DKIM passes): "
        + link("/confirm", {**base, "action": "allow-address"}, "this sender address")
        + " &middot; "
        + link("/confirm", {**base, "action": "allow-domain"}, "this sender's domain")
        + "</p>"
    )
    return "".join(out)


ACTION_TEXT = {
    "teach-spam": "Teach Bayes that this message is spam",
    "teach-ham": "Teach Bayes that this message is not spam",
    "restore-junk": "Move this message back to Junk (the job will keep it)",
    "restore-inbox": "Move this message to the Inbox (the job will keep it)",
    "allow-address": "Allowlist this sender address",
    "allow-domain": "Allowlist this sender's whole domain",
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
}


def batch_page(records: list, missing: int, keys: list, token: str, info: dict) -> str:
    """Show the selected rows and the actions that apply to at least one of them."""
    hidden = f'<input type="hidden" name="token" value="{esc(token)}">' + "".join(
        f'<input type="hidden" name="sel" value="{esc(k)}">' for k in keys
    )
    buttons = []
    for action, text in BATCH_TEXT.items():
        count = sum(1 for r in records if actions_module.eligible(action, r))
        if count:
            buttons.append(
                f'<button type="submit" name="action" value="{esc(action)}">{esc(text)} ({count} of {len(records)})</button>'
            )
    rows = "".join(
        "<tr>"
        f"<td>{esc(r.get('ts'))}</td><td>{esc(r.get('from_addr'))}</td><td>{esc(r.get('subject'))}</td>"
        f"<td>{esc(r.get('decision'))}</td><td>{esc(r.get('folder') or '-')}</td></tr>"
        for r in records
    )
    note = f'<p class="error">{esc(missing)} selected row(s) are no longer in the log and are left out.</p>' if missing else ""
    if buttons:
        actions = (
            f'<form method="post" action="/batch">{hidden}'
            f"<p>{' '.join(buttons)}</p></form>"
            '<p class="meta">Each button applies only to the rows it fits; the count shows how many. '
            "Teaching runs one message at a time, so a large selection takes a while.</p>"
        )
    else:
        actions = "<p>None of the selected messages can be taught or restored (they are gone from Outlook or still in Junk).</p>"
    body = (
        f"<p>{esc(len(records))} message(s) selected.</p>"
        + note
        + actions
        + "<table><thead><tr><th>Time</th><th>Sender</th><th>Subject</th><th>Decision</th><th>Folder</th></tr></thead>"
        f"<tbody>{rows}</tbody></table><p>{link('/', {}, 'Back to the decisions')}</p>"
    )
    return layout("Batch actions", body, header_block(info))


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
