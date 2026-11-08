"""The review page's HTTP server (standard library only).

Reads only /state/logs/ for viewing. State-changing requests are POSTs that carry a per-process
token and, when the browser sends an Origin, must come from the page's own host.
"""

from __future__ import annotations

import hmac
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import logreader, pages
from .actions import ActionError

STATIC = Path(__file__).parent / "static"
MAX_BODY = 4096
MAX_BATCH_BODY = 256 * 1024
MAX_BATCH = logreader.PER_PAGE * 2  # a page is 100 rows; allow some slack
CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
    "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
)


def _float(value: str):
    try:
        return float(value) if value not in ("", None) else None
    except ValueError:
        return None


def filters_from(params: dict) -> logreader.Filters:
    return logreader.Filters(
        decision=params.get("decision", ""),
        date_from=params.get("date_from", ""),
        date_to=params.get("date_to", ""),
        score_min=_float(params.get("score_min", "")),
        score_max=_float(params.get("score_max", "")),
        q=params.get("q", ""),
    )


class Context:
    def __init__(self, log_dir: Path, actions=None, csrf: bool = True):
        self.log_dir = Path(log_dir)
        self.actions = actions  # None when the page runs without the job (actions are unavailable)
        self.csrf = csrf  # check the form token and the Origin on actions (OJF_UI_CSRF)
        self.token = secrets.token_urlsafe(32)


def make_handler(ctx: Context):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ojf-ui"
        sys_version = ""

        # ------------------------------------------------------------ plumbing

        def log_message(self, fmt, *args):  # the container log is for the job
            return

        def _send(self, status: int, body: bytes, content_type: str = "text/html; charset=utf-8", extra=()):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self._security_headers()
            for key, value in extra:
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def _security_headers(self):
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-store")

        def _html(self, html: str, status: int = 200):
            self._send(status, html.encode("utf-8"))

        def _params(self) -> dict:
            query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
            return {k: v[0] for k, v in query.items()}

        # ------------------------------------------------------------ GET

        def do_GET(self):  # noqa: N802
            path = urlsplit(self.path).path
            params = self._params()
            try:
                if path == "/":
                    self._decisions(params)
                elif path == "/message":
                    self._message(params)
                elif path == "/summary":
                    self._html(pages.summary_page(logreader.summary(ctx.log_dir), logreader.header(ctx.log_dir)))
                elif path == "/whatif":
                    self._whatif(params)
                elif path == "/export.csv":
                    self._export(params)
                elif path == "/confirm":
                    self._confirm(params)
                elif path == "/static/style.css":
                    self._send(200, (STATIC / "style.css").read_bytes(), "text/css; charset=utf-8")
                elif path == "/static/batch.js":
                    self._send(200, (STATIC / "batch.js").read_bytes(), "text/javascript; charset=utf-8")
                else:
                    self._html(pages.layout("Not found", "<p>No such page.</p>"), 404)
            except BrokenPipeError:
                pass

        def _decisions(self, params):
            try:
                page_no = max(1, int(params.get("page", "1") or 1))
            except ValueError:
                page_no = 1
            sort = params.get("sort", "time") if params.get("sort") in ("time", "score") else "time"
            order = params.get("order", "desc") if params.get("order") in ("asc", "desc") else "desc"
            page = logreader.query(ctx.log_dir, filters_from(params), sort, order, page_no)
            view = {**params, "page": str(page_no), "sort": sort, "order": order}
            self._html(
                pages.decisions_page(page, view, logreader.header(ctx.log_dir), ctx.token, can_act=ctx.actions is not None)
            )

        def _record(self, params):
            return logreader.find(ctx.log_dir, params.get("id", ""), params.get("ts", ""))

        def _message(self, params):
            record = self._record(params)
            if record is None:
                self._html(pages.layout("Not found", "<p>That record is not in the log (it may have rotated out).</p>"), 404)
                return
            self._html(pages.message_page(record, logreader.header(ctx.log_dir)))

        def _whatif(self, params):
            q, d = _float(params.get("quarantine", "")), _float(params.get("delete", ""))
            info = logreader.header(ctx.log_dir)
            if params.get("quarantine") or params.get("delete"):
                if (params.get("quarantine") and q is None) or (params.get("delete") and d is None):
                    self._html(pages.whatif_page(params, None, "Thresholds must be numbers.", info))
                    return
                if q is not None and d is not None and q >= d:
                    self._html(pages.whatif_page(params, None, "The quarantine threshold must be lower than the delete threshold.", info))
                    return
                self._html(pages.whatif_page(params, logreader.what_if(ctx.log_dir, q, d), "", info))
            else:
                self._html(pages.whatif_page(params, None, "", info))

        def _export(self, params):
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="decisions.csv"')
            self._security_headers()
            self.send_header("Connection", "close")
            self.end_headers()
            for chunk in pages.csv_rows(logreader.all_matching(ctx.log_dir, filters_from(params))):
                self.wfile.write(chunk.encode("utf-8"))
            self.close_connection = True

        def _confirm(self, params):
            action = params.get("action", "")
            record = self._record(params)
            if record is None or action not in pages.ACTION_TEXT:
                self._html(pages.layout("Not found", "<p>Nothing to confirm.</p>"), 404)
                return
            self._html(pages.confirm_page(action, record, ctx.token, logreader.header(ctx.log_dir)))

        # ------------------------------------------------------------ POST

        def do_POST(self):  # noqa: N802
            path = urlsplit(self.path).path
            if path not in ("/act", "/batch"):
                self._html(pages.layout("Not found", "<p>No such page.</p>"), 404)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = -1
            if not 0 <= length <= (MAX_BATCH_BODY if path == "/batch" else MAX_BODY):
                self._html(pages.layout("Refused", "<p>Bad request.</p>"), 400)
                return
            # read the (size-capped) body before any refusal: answering and closing with unread data can reset the
            # client's connection, so it would see a reset instead of the refusal
            fields = parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
            form = {k: v[0] for k, v in fields.items()}
            origin = self.headers.get("Origin")
            if ctx.csrf and origin and urlsplit(origin).netloc != self.headers.get("Host", ""):
                self._html(pages.layout("Refused", "<p>Cross-site request refused.</p>"), 403)
                return
            if ctx.csrf and not hmac.compare_digest(form.get("token", ""), ctx.token):
                self._html(pages.layout("Refused", "<p>This form has expired. Go back and open the action again.</p>"), 403)
                return
            if path == "/batch":
                self._batch(form, fields.get("sel", []))
            else:
                self._act(form)

        def _batch(self, form, selected):
            info = logreader.header(ctx.log_dir)
            back = pages.link("/", {}, "Back to the decisions")
            if ctx.actions is None:
                self._html(pages.result_page("Actions are only available while the job is running.", False, back, info), 503)
                return
            keys = []
            for value in dict.fromkeys(selected):  # drop repeats, keep the order
                ts, sep, message_id = value.partition("|")
                if sep and ts and message_id:
                    keys.append((message_id, ts))
            if not keys:
                self._html(pages.result_page("No rows were selected.", False, back, info), 400)
                return
            if len(keys) > MAX_BATCH:
                self._html(pages.result_page(f"Too many rows selected (the limit is {MAX_BATCH}).", False, back, info), 400)
                return
            found = logreader.find_many(ctx.log_dir, keys)
            records = [found[k] for k in keys if k in found]
            missing = len(keys) - len(records)
            action = form.get("action", "")
            if not action:  # first step: show what was selected and what can be done with it
                values = [f"{ts}|{message_id}" for message_id, ts in keys]
                self._html(pages.batch_page(records, missing, values, ctx.token, info))
                return
            try:
                results = ctx.actions.batch(action, records)
            except ActionError as exc:
                self._html(pages.result_page(str(exc), False, back, info), 400)
                return
            self._html(pages.batch_result_page(action, results, missing, info))

        def _act(self, form):
            info = logreader.header(ctx.log_dir)
            back = pages.link("/message", {"id": form.get("id", ""), "ts": form.get("ts", "")}, "Back to the message")
            record = logreader.find(ctx.log_dir, form.get("id", ""), form.get("ts", ""))
            if record is None:
                self._html(pages.result_page("That record is not in the log.", False, back, info), 404)
                return
            if ctx.actions is None:
                self._html(pages.result_page("Actions are only available while the job is running.", False, back, info), 503)
                return
            action = form.get("action", "")
            try:
                if action == "teach-spam":
                    message = ctx.actions.teach(record, spam=True)
                elif action == "teach-ham":
                    message = ctx.actions.teach(record, spam=False)
                elif action == "restore-junk":
                    message = ctx.actions.restore(record, "junk")
                elif action == "restore-inbox":
                    message = ctx.actions.restore(record, "inbox")
                elif action == "allow-address":
                    message = ctx.actions.allowlist("address", record.get("from_addr", ""))
                elif action == "allow-domain":
                    message = ctx.actions.allowlist("domain", record.get("from_addr", ""))
                else:
                    raise ActionError("Unknown action.")
            except ActionError as exc:
                self._html(pages.result_page(str(exc), False, back, info), 400)
                return
            self._html(pages.result_page(message, True, back, info))

    return Handler


def make_server(port: int, ctx: Context, host: str = "0.0.0.0") -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), make_handler(ctx))
    server.daemon_threads = True
    return server


def serve_in_thread(port: int, ctx: Context) -> ThreadingHTTPServer:
    server = make_server(port, ctx)
    threading.Thread(target=server.serve_forever, name="review-ui", daemon=True).start()
    return server

