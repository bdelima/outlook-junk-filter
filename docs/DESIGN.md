# SpamWatch design

SpamWatch is the product name; the repository, image and environment variables keep the name `outlook-junk-filter` (`OJF_*`).
This document describes what is built as of **0.12.0**, how the parts fit together, and why they are built that way. For
deploying and operating it, see the [runbook](RUNBOOK.md). For settings, see the [README](../README.md).

## Purpose and limits

One container instance watches the **Junk folder of one personal Microsoft mailbox** (`live.com`, `outlook.com`, `hotmail.com`)
through Microsoft Graph. It scores every message it has not handled with SpamAssassin plus a few additions, and decides:
keep, move to a quarantine folder, or delete. A small web page (the *review page*) lets a person see every decision, correct the
filter's learning, restore mail, and manage the history.

Deliberate limits:

- **Junk folder only.** The Inbox and every other folder are never read. The job reads the quarantine folder only to expire old
  messages.
- **Nothing is changed until you say so.** The default mode is `report-only`: scores and would-be decisions are logged,
  nothing is sent to Graph that changes mail.
- **Any doubt keeps the message.** A fetch, scoring or action error, an invalid setting, or a missing setting never deletes.
- **One instance, one mailbox.** There is no account field anywhere. A second mailbox is a second container with its own state.
- **No message bodies are stored.** The log holds metadata, scores and rule names. Bodies are fetched again from Outlook when
  something needs them (teaching, AI rating).
- **amd64 image only.**

## Architecture

```mermaid
flowchart LR
  subgraph Container["outlook-junk-filter container (uid 1000, one process)"]
    loop["Job loop (main thread)"]
    ui["Review page + API (HTTP thread)"]
    sa["spamassassin / sa-learn (subprocess)"]
  end
  graph["Microsoft Graph\n(Junk, Quarantined, Deleted Items)"]
  ai["AI server (optional)\nOpenAI-style /v3 on the LAN"]
  state[("/state: token, delta, bayes,\nlogs, status")]
  cfg[("/config: local.cf,\nojf-allowlist.cf")]
  browser["Browser / Homepage"]
  loop -->|delta, raw MIME, move, delete| graph
  loop --> sa
  loop -->|rate in-range messages| ai
  loop --> state
  ui --> state
  ui -->|re-fetch, move| graph
  ui -->|sa-learn| sa
  ui -->|allowlist lines| cfg
  sa --> cfg
  browser --> ui
```

The job and the review page share **one process**: the page runs in a daemon thread started by the job, so they share the Graph
client, the token cache (behind a lock) and the restored-messages list. If the page cannot start (port in use), the job logs an
error and carries on. `python -m outlook_junk_filter.ui` serves the page alone, read-only, which the image tests use.

| Module | Role |
| --- | --- |
| `__main__.py` | Startup, the loop, sign-in, daily log prune, status file |
| `config.py` | Reads and validates `OJF_*` settings; invalid values fall back to safe defaults and are reported as errors |
| `auth.py` | MSAL public client, device-code flow, token cache in `/state/tokens/token.json` (mode 600), account address |
| `graph.py` | Graph client: delta walk, raw MIME fetch, move, delete, folder lookup/creation, old-message query; retries and throttling |
| `runner.py` | One scan pass: queue, per-message pipeline, decisions, quarantine expiry, run summary |
| `scoring.py` | Runs `spamassassin`, parses its headers, reads Microsoft's SCL header, lints the config |
| `ai.py` | Optional AI rating: text extraction, prompt, strict reply parsing, startup check |
| `state.py`, `restored.py` | Delta link, retry list, handled ids; list of messages the user restored |
| `logs.py` | Container log, `decisions.jsonl` (rotating), tombstones, pruning |
| `status.py` | `/state/status.json`: account, version, mode, last scan time |
| `healthcheck.py` | Container health: `/state/health` touched recently enough |
| `ui/server.py` | HTTP routes, security headers, token and Origin checks |
| `ui/pages.py` | All HTML (everything from the log is escaped) |
| `ui/logreader.py` | Reads the logs: filter, sort, page, find, summary, what-if; hides deleted rows |
| `ui/actions.py` | Teach, restore, allowlist, delete rows, batches |
| `ui/history.py` | Reads `actions.jsonl` back for the *Actions taken* chips |
| `ui/rules.py` | Plain-English rule descriptions |
| `ui/stats.py` | Counts for `GET /api/stats` |

## The scan pass

Every `OJF_INTERVAL_SECONDS` (default 900, minimum 60):

1. **Lint** the SpamAssassin config. If it does not load, nothing is scored and every message is kept (an error is logged).
2. **Token**: silent refresh. If there is none or it is invalid, start a device-code sign-in (see below) and wait for it.
3. **Walk the Junk folder's delta query** (`/me/mailFolders/junkemail/messages/delta`). Pages are requested with
   `Prefer: odata.maxpagesize=50`. (`$top` was used first and capped the walk at 50 messages; the header form pages correctly.)
   A warning is logged if the first walk returns fewer messages than the folder reports.
4. **Build the queue**: the retry list first, then new messages that are not already in the handled list (keyed by
   `internetMessageId`, kept 90 days).
5. **Per message** (see next section). State is saved every 25 messages.
6. **Move the delta link on only after every message is handled or on the retry list**, so a crash replays the pass.
7. **Expire the quarantine folder**: messages whose *received* time is older than `OJF_QUARANTINE_RETENTION_DAYS` are deleted
   the way `OJF_DELETE_MODE` says (act mode only).
8. Log a run summary, the AI and SCL counters, and write `last_scan` to the status file.

### Per-message pipeline

1. **Restored?** If the message's `internetMessageId` is on the restored list: keep (`restored`), never rescore.
2. **Fetch** the raw MIME. A 404 means it left Junk; skip. Other errors: keep and retry (up to 3 attempts, then give up and
   keep, logged as an error).
3. **Score** with `spamassassin --nocreate-prefs --siteconfigpath /config` (no spamd). A scoring failure: keep and retry.
4. **Microsoft SCL bonus**: the header `X-MS-Exchange-Organization-SCL` (present on Graph's copy) maps to points through
   `OJF_MS_SCL_POINTS` (default `7=1.5, 8=3, 9=4`). The added rule is named `OJF_MS_SCL_<n>`.
5. **Allowlist check**: any fired rule matching `OJF_KEEP_RULE_PATTERN` (default `^USER_IN_.*(WHITE|WELCOME)LIST`) means keep.
6. **AI rating** (if configured, and not allowlisted, and the score so far is in range): may add points (see below).
7. **Decide**, in this order:
   - score ≥ `OJF_DELETE_THRESHOLD` → delete (`deleted-items` or `permanent`)
   - else score ≥ `OJF_QUARANTINE_THRESHOLD` (quarantine on) → move to the quarantine folder
   - else keep (`below threshold`, or `no threshold set`)

   In `report-only` the first two are logged as `would-delete` and `would-quarantine` and nothing is sent to Graph.
8. **Record** one JSON line in `decisions.jsonl`, mark handled.

A Graph failure while acting (delete or move) keeps the message and retries it; a network failure aborts the pass and is retried
on the next loop.

`act` mode behaves as `report-only` (with an error every run) when the delete mode is missing or invalid, when no threshold is
set, when the quarantine threshold is not lower than the delete threshold, or when the quarantine folder name collides with a
system folder.

## Scoring

### How SpamAssassin is run and configured

The job calls `spamassassin` as a subprocess per message with `--siteconfigpath /config`. That path *replaces* Debian's
`/etc/spamassassin`, where the `.pre` files that load the plugins live, and without them nothing can be scored. So the
Dockerfile copies the `.pre` files into SpamAssassin's *sys rules directory* (`/var/lib/spamassassin/<version>` after
`sa-update`, else `/usr/share/spamassassin`), and the build fails if the Check plugin still cannot load.

The same directory receives `rules/ojf-rules.cf` as `99_ojf.cf`. Files in the sys rules directory load **before** `/config`, so
`/config/local.cf` can override any shipped score (for example `score OJF_ED_SPAM 0`). (A `--cf` include loads *after*
`/config` and cannot be overridden; the first attempt used it and was replaced for that reason.)

Rules are fixed when the image is built (`sa-update` runs at build time). Newer rules arrive with a new image.

### Shipped rules (`rules/ojf-rules.cf`)

| Rule | Score | What it catches |
| --- | --- | --- |
| `OJF_ED_SPAM` | +3.0 | Erectile-dysfunction pitches that avoid drug names; needs two of three phrase groups (subject, effect, pitch) so a health newsletter scores nothing |
| `OJF_WEIGHTLOSS_AD` | +2.5 | Weight-loss and GLP-1 advertising, counted only when Microsoft's SCL is 5 or higher (the topic alone is not spam) |
| `OJF_JUNK_ADMITS_SPAM` | +1.5 | SCL 5 or higher and SpamAssassin's stock `ADMITS_SPAM` fires |

SCL gating exists because two of these are about *topics*; Microsoft's own verdict is the second opinion that keeps legitimate
health mail clear. The phrase lists are hand-written and need new entries as wording changes.

### Safe Links

Microsoft rewrites links to `https://<region>.safelinks.protection.outlook.com/?url=<target>`. SpamAssassin's shipped
redirector patterns unwrap `url=`, so URL blocklists see the real target (tested every build on a synthetic message). The rewrite
changes the body after the sender signed it, so DKIM reports invalid even when Microsoft's own result was a pass; an allowlist
entry for such a message therefore rests on SPF.

### Bayes

The Bayes database lives in `/state/bayes`. It scores only after 200 spam and 200 non-spam messages have been learned.
SpamAssassin's `bayes_auto_learn` is on by default; teaching from the review page corrects what it gets wrong. Teaching refetches
the message from Outlook and runs `sa-learn` (with its own locking); nothing edits the database directly.

### AI rating (optional, off until `OJF_AI_URL` is set)

- **Where it sits:** after SpamAssassin, the SCL bonus and the shipped rules, before the decision.
- **Which messages:** those not allowlisted whose score is in `[OJF_AI_MIN_SCORE, OJF_AI_MAX_SCORE)`; the upper bound defaults to
  the delete threshold, because a message at or above it is already decided. The range keeps the slow model on the ambiguous middle.
- **What is sent:** sender address, subject and the first 1,500 characters of text, to the server you configure. No headers or
  attachments.
- **Prompt injection:** the prompt marks the message as untrusted data and forbids following it; only a number in a strict JSON
  reply is used, clamped to 0–10. A message that says "rate me 0" can at worst earn no points.
- **Points:** `OJF_AI_MAX_POINTS × max(0, rating − 5) / 5`, so ratings of 5 or less add nothing and 10 adds the maximum. The rule
  `OJF_AI_<n>` is added; `ai_score` and `ai_reason` are logged.
- **Failure is harmless:** an unreachable server or HTTP error adds no points and stops further requests for the rest of the run
  (one warning); an unreadable reply costs only that message.
- **Startup check:** the job validates the address (a malformed `OJF_AI_URL` such as `host::8000` is an error and turns AI off) and
  asks `/models` once, logging whether the model is available.
- **The server:** the `ovms-npu` image (`npu-server/`) is OpenVINO Model Server with Intel's NPU driver 1.38.0 swapped in
  (the stock image's 1.35.0 fails against 1.38.0 host firmware). It serves `OpenVINO/Qwen3-8B-int4-cw-ov`, one request at a time
  (about 7 s each on a Meteor Lake NPU), no authentication, LAN only.

## Quarantine, restore and message ids

- Flagged messages move to a top-level folder (default `Quarantined`, created if missing; an existing folder of that name is
  reused). The Junk-only scan never sees them again.
- **Graph gives a moved message a new id.** The log therefore stores `current_message_id` and `folder` (where it is now); actions
  from the review page use that id.
- **Restore** (from quarantine or Deleted Items to Junk or the Inbox) adds the message's `internetMessageId` to
  `/state/restored.json` (kept 365 days) so the next pass keeps it rather than rescoring and moving it back.
- A permanently deleted message is gone: its row can be viewed, not taught or restored.

## The review page

Served by the job's process on `OJF_UI_PORT` (default 60888). **There is no login.** It is meant for a private network or
behind a reverse proxy.

### Security model

- Everything from the log (senders, subjects, AI reasons, rule names, the mailbox address) is escaped; nothing builds markup by
  concatenating a field. Subjects and senders come from phishing mail.
- Content-Security-Policy: `default-src 'none'`; scripts, styles and images only from the page's own origin; no framing. The page
  makes no external requests. Its script (`ui.js`) is a static file (select-all, selection bar, column resizing).
- State changes are POSTs capped at 4 KB (256 KB for batches, 200 rows at most). With `OJF_UI_CSRF=on` each also needs the
  per-process form token and a matching `Origin`. Off by default, because the page is for a private LAN.
- CSV export prefixes cells that begin with `=`, `+`, `-` or `@`.
- The page reads only `/state/logs/` and `/state/status.json`. It writes `actions.jsonl`, the tombstone file, the allowlist file
  and the restored list, always through the job's own code paths.
- `GET /api/stats` is open like everything else and returns counts only.

### Routes

| Route | Purpose |
| --- | --- |
| `GET /` | Decisions: filters, sort by any column, pager, selection bar, *Actions taken* column, score bubble |
| `GET /message` | One record: every field, rules with descriptions, Actions panel with done/not-done indicators |
| `GET /summary`, `/whatif`, `/export.csv` | Per-day counts, SCL table and histogram; threshold what-if; CSV of the filtered view |
| `GET /confirm`, `POST /act` | Single action with a confirmation page |
| `POST /batch` | Batch: first step shows the selection and the actions that fit; second step runs one |
| `POST /delete-matching` | Delete every row a filter matches (recounts before deleting) |
| `GET /api/stats` | JSON counts for Homepage, cached 30 s |
| `GET /static/{style.css,ui.js,icon.svg}` | Assets |

### Actions

| Action | Effect | Recorded |
| --- | --- | --- |
| Teach spam / not spam | Re-fetch the message, `sa-learn --spam/--ham` | `actions.jsonl` |
| Restore to Junk / Inbox | Graph move; add to the restored list | `actions.jsonl` |
| Allowlist address / domain | Add `whitelist_auth <entry>` to `/config/ojf-allowlist.cf` (counts only when SPF or DKIM passes) | `actions.jsonl` (with the message id) |
| Delete from log | Hide the row now; remove it from the files at the next prune. **Nothing in Outlook is touched** | `actions.jsonl` (one line per row, or one summary line above 200) |

In a batch, an action runs on the rows it fits and skips the rest with a reason; one failure does not stop the others; a message
selected twice (or logged twice) is acted on once; allowlist entries are de-duplicated per sender or domain.

*Actions taken* chips are read from `actions.jsonl`. Teach and restore belong to the message id they were done under; an
allowlist entry shows on every row from that sender.

### Plain-English rules

The bubble over a score and the message page describe fired rules in words. Sources, in order: a built-in phrase list for common
rules (and `OJF_MS_SCL_n`, `OJF_AI_n`), then SpamAssassin's own `describe` text read from the rule files in the image, then the
raw name. SpamAssassin's stock scores order the phrases (the log stores names, not points) and decide *Against it* versus
*In its favor*; neutral rules are hidden from the bubble. At most five findings against and three in favor show, the rest counted.

### Logs, deletion and pruning

- `decisions.jsonl` rotates at 10 MB × 10 files. Each line is one scored message.
- **Delete** appends `(message_id, ts)` to `/state/logs/deleted.jsonl`; the reader hides those rows immediately. The tombstone
  file is written under a lock shared with the prune.
- **Prune** runs in the job thread at startup and then every 24 hours, when nothing else is appending. It closes the log handler
  (so no row goes to a replaced file), rewrites each file without rows older than `OJF_LOG_RETENTION_DAYS` (default 30; `0`
  keeps all) or deleted by tombstone, removes emptied rotated files, trims old teach/restore/delete entries from
  `actions.jsonl` (allowlist entries are kept so a sender's chip survives), and clears the tombstone file. A failure is logged and
  never stops the job.

### Header, icon, status and stats

- The header shows the name, the **mailbox address** and an icon (also the tab icon, `/static/icon.svg`). The address comes from the
  MSAL sign-in record, so no extra Microsoft permission is needed. The job stores it, with the version, mode and last-scan time, in
  `/state/status.json`; the page and stats read it from there.
- `GET /api/stats` returns `app`, `version`, `account`, `mode`, `healthy`, `last_scan_seconds_ago`, `today` and `last_7_days`
  (`scored`, `kept`, `quarantined`, `deleted`, `errors`, `ai_rated`) and `log_records`. Counts use the latest record per
  message in each window; "today" is the container's local day (`TZ`); report-only decisions count under their would-be class.

## State and files

| Path | Written by | Notes |
| --- | --- | --- |
| `/state/tokens/token.json` | auth | MSAL cache, mode 600, atomic replace. A key to the mailbox |
| `/state/delta/state.json` | runner | Delta link, retry list, handled ids; atomic replace |
| `/state/bayes/` | SpamAssassin, `sa-learn` | Own locking |
| `/state/logs/decisions.jsonl*` | runner, prune | Rotating, pruned |
| `/state/logs/actions.jsonl` | review page | Audit of page actions |
| `/state/logs/deleted.jsonl` | review page | Tombstones, cleared by the prune |
| `/state/restored.json` | review page | Restored messages, 365 days |
| `/state/status.json` | job | Account, version, mode, last scan |
| `/state/health` | job | Touched during work; health check reads its age (window: the larger of three intervals and 30 minutes) |
| `/state/home`, `/state/tmp` | sa-learn, SpamAssassin | The container has no home directory; HOME and TMPDIR point here |
| `/config/local.cf` | you | Never written by the job |
| `/config/ojf-allowlist.cf` | review page | `whitelist_auth` lines; SpamAssassin loads every `.cf` in `/config` |

The container runs as uid/gid 1000 on a read-only root filesystem; only `/state` and `/config` are written.

## Sign-in

MSAL device-code flow against the `consumers` authority with the delegated scope `Mail.ReadWrite`. The Application (client) ID
belongs to an app registration for *personal Microsoft accounts*, set as a public client. A client ID is only an app
registration; each mailbox signs in separately and its token stays in that instance's `/state`. The job logs
`SIGN-IN NEEDED: open <url> and enter code <code>`, waits for the code (touching the health file so the container stays
healthy), and refreshes silently afterwards. A refresh failure that means the token is no longer valid starts a new sign-in.

## Failure behavior

| Situation | Result |
| --- | --- |
| Fetch, score or action fails for one message | Kept, retried (3 attempts), then kept for good; logged as an error row |
| Network or Graph failure mid-pass | Pass skipped; delta link not advanced; retried next loop |
| Process crash mid-pass | Pass replays; handled ids prevent double work |
| Config invalid | Defaults applied, error logged; deletion never follows from invalid settings |
| SpamAssassin config does not load | Nothing scored, everything kept |
| AI server down or slow | No AI points; one warning per run |
| Token expired | New device-code sign-in; scanning resumes after it |
| Review page cannot start | Error logged; the job runs without it |
| Prune fails | Logged; the job continues |

## Decision log

- **One instance, one mailbox.** Earlier versions had account slots; removing them removed a whole class of mix-ups. A second
  mailbox is a second container (its own token, state, Bayes and allowlist), optionally sharing the app registration.
- **`Prefer: odata.maxpagesize=50` instead of `$top`.** `$top` on the delta query stopped the walk at 50 messages.
- **Subprocess SpamAssassin, no spamd.** Simpler and isolated; the cost (a process per message) is fine at personal volumes.
- **Shipped rules in the sys rules directory.** So users can override a score in `/config/local.cf`.
- **Microsoft's SCL as a score bonus and a gate.** The header is on Graph's copy of every message; it rescued obvious spam that
  scored 1.2 and keeps topic rules from firing on legitimate mail.
- **AI is ranged, additive and off by default.** It never decides alone; a failure cannot cause a deletion.
- **Tombstones for deleting rows.** The job appends to the log while the page runs; rewriting from the page could lose a row. The
  daily prune, in the writer's thread, does the real rewrite.
- **No login on the review page.** Chosen for a private LAN behind a proxy. `OJF_UI_CSRF` adds token and Origin checks. The stats
  endpoint follows the same rule and carries counts only.
- **Plain-English rules from a curated list plus SpamAssassin's own text**, not from the AI: deterministic, instant and free.
- **amd64 only**, built in CI; no local Docker needed or used.

## Testing, CI and release

- Unit tests (pytest) mock Graph and SpamAssassin and use synthetic `.eml` fixtures; ruff runs in CI.
- `tests/image/test-image.sh` runs against the built image: SpamAssassin loads as an arbitrary non-root uid, fixtures score as
  expected, Safe Links unwrap works offline, each shipped rule fires and stays quiet where it should (and can be overridden),
  `sa-learn` writes Bayes on a read-only root, and the review page serves an escaped Decisions page with a CSP.
- Release: bump `VERSION` in a reviewed PR; merging triggers the release workflow, which waits for approval in the `release`
  Environment, then pushes `bdelima/outlook-junk-filter:<version>` and `:latest` (amd64), and creates the tag and GitHub
  release last. Re-running a release for an existing tag is skipped unless forced.
- The second image, `bdelima/ovms-npu`, has its own CI and publish workflows (`npu-server/VERSION`, tag `ovms-npu-<version>`).

### Version history

| Version | What changed |
| --- | --- |
| 0.1.0 | Repository scaffold |
| 0.2.0 | Junk-folder scorer, image, tests and CI; first image release |
| 0.3.0 | Quarantine folder with retention expiry; the review page (decisions, summary, what-if, CSV, teach, allowlist, restore) |
| 0.3.1 | Fix: the delta walk stopped at 50 messages (page size now set with a `Prefer` header) |
| 0.4.0 | One instance, one mailbox |
| 0.5.0 | Microsoft SCL bonus |
| 0.6.0 | Shipped rules: ED spam without drug names |
| 0.7.0 | SCL-gated weight-loss and admits-spam rules; SCL view |
| 0.8.0 – 0.8.1 | AI scoring; startup validation and model check |
| 0.9.0 | Batch teach and restore |
| 0.10.0 | Allowlist batches, *Actions taken*, plain-English rules, bubble, resizable columns, one-row toolbar |
| 0.11.0 | SpamWatch header, icon and mailbox; delete rows; log pruning; stats API |
| 0.12.0 | Delete all matching rows; batch actions return to the filtered view |

## Not built

Alerting beyond log lines and the health check; a login for the review page; moving wanted bulk mail out of Junk (only flagged
messages move); Razor, Pyzor and DCC; a separate spamd container; scheduled rebuilds; shared learning between two instances
(sharing a Bayes directory between containers is untested).
