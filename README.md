# SpamWatch (outlook-junk-filter)

Scores the Junk folder of one personal Outlook mailbox with SpamAssassin, adds Microsoft's own verdict and an optional
local AI opinion, and quarantines or deletes obvious spam and phishing. A review page lets you see why, teach the filter,
restore mail and manage the history.

SpamWatch is the name you see on the page; the repository, image and `OJF_*` settings keep the name `outlook-junk-filter`.

One long-lived container reads the **Junk folder only** of one personal Microsoft mailbox (a `live.com` or
`outlook.com` address) through Microsoft Graph, scores each message with SpamAssassin, and, in `act` mode, deletes messages
at or above a score you choose. Wanted bulk mail stays where it is. The Inbox and every other folder are never read
or changed. The only inbound traffic is the [review page](#the-review-page) (one port, no login: put it behind a reverse proxy).

One container instance runs exactly one mailbox. It starts in `report-only` mode, where no mail is changed. Nothing is
deleted until you have reviewed the scores and switched to `act`.

## Documentation

- [docs/RUNBOOK.md](docs/RUNBOOK.md): deploy, go live, upgrade and roll back, operate, second mailbox, Homepage, troubleshooting, backup, release.
- [docs/DESIGN.md](docs/DESIGN.md): architecture, the scan pipeline, scoring, the review page, state files, failure behavior, decisions, version history.
- [npu-server/README.md](npu-server/README.md): the `ovms-npu` image that serves the optional AI scoring model.
- This README: what it does, every setting, the review page, and rollout advice.

## How it works

- Each loop (every `OJF_INTERVAL_SECONDS`) walks the Junk folder's Graph delta
  query, fetches each new message as raw MIME, scores it with `spamassassin --siteconfigpath /config` (no spamd),
  decides, and logs.
- Decision, in order: a fired rule matches `OJF_KEEP_RULE_PATTERN` (allowlisted) means keep; a message that could not
  be scored means keep and retry (3 attempts, then give up and keep); a score at or above `OJF_DELETE_THRESHOLD`
  means delete; otherwise a score at or above `OJF_QUARANTINE_THRESHOLD` means move to the quarantine folder; anything
  else means keep. In report-only mode these are logged as `would-delete` and `would-quarantine` and nothing is sent
  to Graph.
- **Quarantine.** Flagged messages are moved out of Junk into a top-level folder of the same mailbox (`Quarantined` by
  default), so you can review them in Outlook. The job still scans only Junk. It reads the quarantine folder only to
  delete messages whose received time is older than `OJF_QUARANTINE_RETENTION_DAYS`, the way `OJF_DELETE_MODE` says.
  Restore a wrongly flagged message by moving it back in Outlook. There is no per-run cap on deletions or moves.
- **Extra evidence beyond SpamAssassin.** Microsoft's own spam verdict (the SCL header) adds points through
  `OJF_MS_SCL_POINTS`; a few shipped rules catch campaigns the stock rules miss; and, if you set `OJF_AI_URL`, a local
  language model rates the ambiguous middle. These points are added to SpamAssassin's score, and the thresholds compare the total. See [AI scoring](#ai-scoring) and [Extra rules](#extra-rules-shipped-in-the-image).
- Any error fetching, scoring or acting on a message keeps it. Invalid settings never cause deletion.
- The delta link is saved only after every message in the pass is handled or on the retry list, so a crash replays
  the pass instead of skipping mail.

## Before you deploy

1. **Register an app** for personal Microsoft accounts: Microsoft's app registration portal, new registration,
   *Personal Microsoft accounts only*. Leave the redirect URI blank, set *Allow public client flows* to *Yes*, add the
   Microsoft Graph **delegated** permission `Mail.ReadWrite`, and copy the *Application (client) ID*.
2. **Create the host directories** once (owned by uid/gid 1000, mode 700):

   ```bash
   sudo install -d -m 700 -o 1000 -g 1000 /opt/outlook-junk-filter /opt/outlook-junk-filter/state /opt/outlook-junk-filter/config
   ```

3. **Create `/opt/outlook-junk-filter/config/local.cf`** from [`config/local.cf.example`](config/local.cf.example).
   It holds your SpamAssassin rules, allowlist and trust path. It is yours: it never goes into the image or this repo.

## Deploy as a Portainer stack

Add a stack with [`docker-compose.example.yml`](docker-compose.example.yml). The only published port is the review page
(`OJF_UI_PORT`, 60888 by default); no network is declared. Set the variables in Portainer's **Environment variables** panel, not in a `.env` file.

| Variable | Required | Default | Meaning |
| --- | --- | --- | --- |
| `OJF_CLIENT_ID` | yes | none | Application (client) ID of your app registration (not a secret). The process exits with a clear message if it is missing. |
| `OJF_INTERVAL_SECONDS` | no | `900` | Seconds between runs. Values below 60 are raised to 60 and logged. |
| `OJF_MODE` | no | `report-only` | `report-only` or `act`. |
| `OJF_DELETE_MODE` | when `OJF_MODE` is `act` | none | `deleted-items` (recoverable in Outlook) or `permanent` (not recoverable). Neither is a default. Also used when quarantined messages expire. |
| `OJF_DELETE_THRESHOLD` | when `OJF_MODE` is `act`, unless `OJF_QUARANTINE_THRESHOLD` is set | none | Total score, as a decimal, at or above which a message is deleted. The total is SpamAssassin's score plus any Microsoft SCL points, shipped-rule points and AI points. |
| `OJF_QUARANTINE_ENABLED` | no | `on` | `on` or `off`. Turns the quarantine feature on or off. |
| `OJF_QUARANTINE_FOLDER` | no | `Quarantined` | Name of the top-level folder flagged messages are moved to. Any name works, except a system folder such as Junk or Inbox. Created if missing; an existing folder of that name is reused. |
| `OJF_QUARANTINE_THRESHOLD` | no | none | Total score (as for `OJF_DELETE_THRESHOLD`) at or above which a message is moved to the folder. Must be lower than `OJF_DELETE_THRESHOLD` when both are set. With only this set, nothing is deleted at score time. |
| `OJF_QUARANTINE_RETENTION_DAYS` | no | `7` | Days a message stays in the folder before the job deletes it. The age counts from the message's received time, so one already older than this is deleted on the next pass. |
| `OJF_LOG_RETENTION_DAYS` | no | `30` | Days of decision history kept. Older rows are pruned daily; `0` keeps everything. |
| `OJF_UI_PORT` | no | `60888` | Port of the review page (the stack publishes it). |
| `OJF_UI_CSRF` | no | `off` | `on` makes the review page's actions require a per-process form token and refuse a request whose `Origin` is not the page's own host (the proxy must then pass `Host` through unchanged). Off by default: the page has no login and is meant for a private LAN. |
| `OJF_MS_SCL_POINTS` | no | `7=1.5,8=3,9=4` | Points added to the score from Microsoft's own spam verdict, the `X-MS-Exchange-Organization-SCL` header (0-9; 5-6 spam, 7-9 high-confidence spam). A message Microsoft rates 8 gets +3 on top of SpamAssassin's score and is logged with the rule `OJF_MS_SCL_8`. `off` disables it. |
| `OJF_AI_URL` | no | none (AI scoring off) | Address of an OpenAI-compatible chat server on your network, for example `http://ojochal:8000` (the [`ovms-npu`](npu-server/README.md) image). A bare host and port gets `/v3` added; a URL with its own path is used as given. See [AI scoring](#ai-scoring). |
| `OJF_AI_MODEL` | no | `OpenVINO/Qwen3-8B-int4-cw-ov` | Model name sent to the server. |
| `OJF_AI_MAX_POINTS` | no | `4` | Most points the AI can add (at a rating of 10; ratings of 5 or less add nothing). |
| `OJF_AI_MIN_SCORE` | no | `0` | Only messages whose score so far is at least this go to the AI. |
| `OJF_AI_MAX_SCORE` | no | the delete threshold | ... and below this. Messages at or above it are already decided. |
| `OJF_AI_TIMEOUT_SECONDS` | no | `60` | Time allowed per rating (the first request after the server starts is slow). |
| `OJF_KEEP_RULE_PATTERN` | no | `^USER_IN_.*(WHITE\|WELCOME)LIST` | Regular expression. A message that fired any matching rule is always kept. |
| `TZ` | no | `UTC` | Timezone of log timestamps. |

An empty value counts as unset. An unrecognized mode is treated as `report-only` and logged as an error. A mode of
`act` makes the job behave as `report-only`, and logs an error on every run, when `OJF_DELETE_MODE` is missing or
invalid, when neither `OJF_DELETE_THRESHOLD` nor `OJF_QUARANTINE_THRESHOLD` is set (a quarantine threshold counts only
while quarantine is on), when the quarantine threshold is not lower than the delete threshold, or when
`OJF_QUARANTINE_FOLDER` names the Junk folder or another system folder.

**A second mailbox** is a second container with its own folders, port and token. The runbook has the compose service and the
steps: [A second mailbox](docs/RUNBOOK.md#5-a-second-mailbox).

### First sign-in

On first start there is no token, so the job logs a line like this and waits:

```text
SIGN-IN NEEDED: open https://www.microsoft.com/link and enter code ABCD1234
```

Read it in Portainer's **Logs** view for the container, open the URL, enter the code, sign in with the mailbox's primary address
and approve. Do this once. The token is saved under `/state/tokens` (mode 600) and refreshes silently each run. If
a code expires first, the job logs that and issues a fresh one.

### What lives where

| Path in the container | Holds |
| --- | --- |
| `/config/local.cf` | SpamAssassin configuration, owned by you and hand-edited. The job never writes here |
| `/config/ojf-allowlist.cf` | Allowlist entries added from the review page (`whitelist_auth` lines). SpamAssassin loads every `.cf` in `/config` |
| `/state/tokens/token.json` | MSAL token cache, mode 600. **This is a key to your mail: back it up like a secret, never commit it.** |
| `/state/delta/state.json` | Delta link, retry list, handled message ids (entries older than 90 days are dropped) |
| `/state/bayes/` | SpamAssassin's Bayes database. It scores only after 200 spam and 200 non-spam messages have been learned |
| `/state/logs/decisions.jsonl` | One JSON object per message, rotated by size (10 files of 10 MB) and pruned by age (`OJF_LOG_RETENTION_DAYS`) |
| `/state/logs/actions.jsonl` | What was done from the review page: time, action, message id, target (never a body). Old teach, restore and delete entries are pruned; allowlist entries are kept |
| `/state/logs/deleted.jsonl` | Rows deleted from the review page, hidden at once and removed from the log files by the next prune |
| `/state/status.json` | Mailbox address, version, mode and last scan time, kept by the job for the page header and `/api/stats` |
| `/state/restored.json` | Messages restored from the review page; the job keeps them (reason `restored`) |
| `/state/health` | Touched by the job; the container health check reads it |
| `/state/home`, `/state/tmp` | `HOME` and `TMPDIR` for SpamAssassin and `sa-learn` (the container has no home directory) |

The container runs as uid/gid 1000 with no home directory and works with a read-only root filesystem (only `/state`
and `/config` are written; the job itself never writes to `/config`, only the review page's allowlist action does). It needs outbound HTTPS to `graph.microsoft.com` and `login.microsoftonline.com`, plus DNS.

## The review page

A web page served by the job's own process (same container, the port you publish with `OJF_UI_PORT`, default 60888). The header
shows the name, the **mailbox address** it watches and an icon (also the browser tab icon). There is **no login**: reach it through
Nginx Proxy Manager or keep the port on a trusted network. It reads only `/state/logs/` and the status file, makes no external
requests, and escapes everything it shows, because senders and subjects come from phishing mail.

### Decisions

- A table of every scored message: time, sender, subject, score, Microsoft SCL, decision, and **Actions taken**.
- **Filter** by decision, date range and score range, and search sender and subject; **sort** by clicking a column heading
  (time, sender, subject, score, SCL, decision). 100 rows per page. **Export CSV** follows the filters (cells that begin with
  `=`, `+`, `-` or `@` are prefixed so a spreadsheet does not run them).
- **Decision values:** `keep`, `quarantined`, `deleted` (moved to Deleted Items), `permanently-deleted`, `error` (kept; will retry or gave up), and, in
  `report-only` mode only, `would-quarantine` and `would-delete`: what the job *would* have done. Nothing was changed in the mailbox.
  A score or SCL that was not recorded shows `N/A`.
- **Resize columns** by dragging a column edge (double-click resets); widths are remembered in the browser.
- **Hover a score** (or Tab to it) for a bubble: the decision and why; the rules that fired in plain English, as *Against it*
  and *In its favor* (strongest first, the rest counted); and the AI rating with its reason. Wording comes from a built-in
  phrase list, then SpamAssassin's own rule descriptions.
- **Actions taken** shows chips for what was already done: *Spam*, *Not spam*, *Restored · Inbox/Junk*, and *Address* or
  *Domain* when the sender is allowlisted. A chip belongs to the message's id in Outlook, so a message that moved and was logged
  again under a new id shows no chip on the newer row.

### Message page

Every field of the log record, every fired rule with a description and its original name, and an **Actions** panel showing each
action as done (with its time) or not done. A done action stays clickable to repeat it. The log holds no bodies: to read a
message, open it in Outlook (in Junk or the quarantine folder).

### Actions

Each runs on one message from the message page (with a confirmation click) or on many from the Decisions page, and is recorded
in `actions.jsonl`.

- **Teach Bayes** as spam or not spam: the job fetches the message from Outlook again and runs `sa-learn` against the Bayes
  database in `/state`. Works for any message still in Outlook (Junk, quarantine or Deleted Items). Teach wanted bulk such as
  Reddit and Substack as *not spam*. A permanently deleted message is gone, so that row cannot be taught.
- **Restore** a message from the quarantine folder or Deleted Items to Junk or the Inbox. The job then keeps it and never
  rescores it.
- **Allowlist** a sender address or domain: adds a `whitelist_auth` line to `/config/ojf-allowlist.cf`, so it counts only when
  SPF or DKIM passes. Microsoft Safe Links rewrites the links in many messages (see [Safe Links](#safe-links)), which breaks the
  sender's DKIM signature, so for those messages only SPF can pass.
- **Delete from log**: removes history rows from the review page only. Nothing in Outlook changes and the job keeps what it
  already decided. The rows disappear at once and are removed from the files by the next prune.

### Batch actions

Tick rows (the header box ticks the page; the selection bar appears once something is ticked), open **Actions ▾**, and choose:
teach as spam or not spam, restore to Junk or Inbox, allowlist address or domain, or delete from log. A confirmation page shows how
many of the selected rows the action fits; the rest are skipped and listed with the reason, one failure does not stop the others,
and a message selected twice is acted on once. Teaching runs one message at a time, so a large selection takes a while. At most
200 rows per batch; ticks reset when the page reloads. After a batch you return to your filtered and sorted view.

**Delete all N matching…** (in the bar above the table) removes every row the current filter matches, or the whole log when there
is no filter. Its confirmation page shows the count and the filter, recounts when you confirm, and asks again if the number changed.

### Summary, What-if and pruning

- **Summary:** counts per decision per day, a table by Microsoft SCL (messages, average and highest score), and a score
  histogram. The status line shows how many records are loaded and how old the newest is.
- **What-if:** type a quarantine and a delete threshold; the page counts how many logged messages would be kept, quarantined or
  deleted, using the logged scores and allowlist hits. It saves nothing, and the scores do not reflect Bayes learning since.
- **Pruning:** at startup and then once a day the job drops decision rows older than `OJF_LOG_RETENTION_DAYS` (default 30; `0`
  keeps them), rows you deleted, and teach, restore and delete records of that age. Allowlist records are kept so a sender's chip
  stays. One line in the container log says what was removed.
- **Auto-learn:** SpamAssassin's own `bayes_auto_learn` is on by default (`config/local.cf.example` carries the line, and `0`
  turns it off). Manual teaching works either way and corrects what auto-learn gets wrong.

### Stats API for Homepage

`GET /api/stats` on the review port returns JSON with counts only (no senders or subjects): `app`, `version`, `account`, `mode`,
`healthy`, `last_scan_seconds_ago`, `today` and `last_7_days` (`scored`, `kept`, `quarantined`, `deleted`, `errors`, `ai_rated`)
and `log_records`. Counts use the latest record per message; "today" is the container's local day (`TZ`); in report-only mode
the would-be decisions count. Like the rest of the page it has no login. Results are cached for 30 seconds. A Homepage entry:

```yaml
- SpamWatch:
    icon: http://HOST:60888/static/icon.svg
    href: http://HOST:60888
    widget:
      type: customapi
      url: http://HOST:60888/api/stats
      mappings:
        - { field: { today: kept }, label: Kept today }
        - { field: { today: quarantined }, label: Quarantined }
        - { field: { today: deleted }, label: Deleted }
        - { field: last_scan_seconds_ago, label: Last scan, format: duration }
```

## Roll it out safely

1. Leave `OJF_MODE` unset (`report-only`). The first run walks the whole Junk folder and logs a score and a would-delete
   verdict for each message; nothing is changed.
2. **Tune it.** Read the per-message log. Confirm Microsoft's `Received` headers record the connecting IP, that
   `auth_results_present` is true, and that `first_untrusted_ip` is the real sender's. Then set `trusted_networks` in
   `local.cf` to the Microsoft hops that appear in your own samples (set only `trusted_networks`; `internal_networks`
   defaults to it). Build the allowlist from sender domains that pass SPF or DKIM and carry `List-Unsubscribe`.
3. Choose the threshold where almost nothing wanted lies above it. SpamAssassin's default spam mark is 5.0, but its
   documentation advises deleting automatically only at exceptionally high scores such as 15.0 or higher.
4. Set `OJF_DELETE_MODE` (start with `deleted-items`) and a threshold: `OJF_QUARANTINE_THRESHOLD` to start by moving
   flagged mail to the quarantine folder (the gentlest way to build confidence), and later `OJF_DELETE_THRESHOLD`.
   Set `OJF_MODE=act`, update the stack, and check the quarantine folder by hand.

To bring in newer SpamAssassin rules, update the stack with force-rebuild: rules are fixed when the image is built.

### AI scoring

Optional and off until `OJF_AI_URL` is set. After the normal pipeline has scored a message (SpamAssassin, the Microsoft SCL bonus,
the shipped rules), a message whose score is inside `OJF_AI_MIN_SCORE` to `OJF_AI_MAX_SCORE` (and not allowlisted) is rated 0 to 10 by
the language model at that address. A rating above 5 adds points, up to `OJF_AI_MAX_POINTS` at 10. The rating and the model's one-line
reason are stored in the log (`ai_score`, `ai_reason`, shown on the message page) and a high rating adds the rule `OJF_AI_<n>`.

- **What is sent:** the sender address, the subject and the first 1,500 characters of the body text, to the address you set. No
  attachments or headers. Nothing leaves your network if the server is on it.
- **Untrusted input:** the prompt tells the model the message is untrusted and not to follow it, and only the number in the reply is
  used. A message that tells the model to rate it 0 can at worst earn no points.
- **Failure is harmless:** if the server is unreachable or answers with an error, the message gets no AI points, and the job stops
  asking until the next run (it logs one warning). An unreadable reply costs only that message.
- **At startup** the job checks the address and asks the server for its model list, and logs one line: `AI scoring server ...: model ... is available`, or an `ERROR` if the address is malformed or the server does not answer. A malformed `OJF_AI_URL` (for example `host::8000`) is reported as an error and AI scoring stays off.
- **Each run logs** `AI rated N of M scored messages (score range ... ), K failed`.
- **Speed:** about 7 seconds per message on an Intel Meteor Lake NPU, one at a time. The score range keeps that to the ambiguous middle.

### Extra rules shipped in the image

`rules/ojf-rules.cf` is installed into the image next to SpamAssassin's stock rules and loaded on every scoring run; your own
rules stay in `/config/local.cf`, which loads after it and can override a score. It currently holds `OJF_ED_SPAM` (+3.0): erectile-dysfunction spam that describes the effect
("rock-hard for hours", "blood flow", "no prescriptions") instead of naming a drug, so the stock pharmacy rules miss it. It
fires only when two of three phrase groups match (subject, effect, pitch), so a health newsletter that mentions blood flow
scores nothing. To switch it off, add `score OJF_ED_SPAM 0` to `/config/local.cf` (the image test checks this works).
It also holds `OJF_WEIGHTLOSS_AD` (+2.5): weight-loss and GLP-1 advertising, counted only when Microsoft's own filter already
rated the message SCL 5 or higher, because the topic alone is not spam. `OJF_JUNK_ADMITS_SPAM` (+1.5) needs no topic at all: Microsoft rated the message SCL 5 or higher and it admits to being
an advertisement (SpamAssassin's stock `ADMITS_SPAM`). The phrase lists are hand-written and will need new
entries as spammers change their wording.

### Safe Links

Microsoft Safe Links rewrites links in incoming mail to `https://<region>.safelinks.protection.outlook.com/?url=<encoded
target>&data=...`. What that means here:

- **URL blocklists still see the real destination.** SpamAssassin's shipped redirector patterns include one that matches
  the `url=` parameter, so it looks up the target's domain, not the Safe Links host. The image test checks this on a
  synthetic message (`tests/fixtures/safelinks-wrapped.eml`) every build, offline.
- **DKIM does not survive.** The rewrite changes the message body after the sender signed it, so the signature's body hash
  no longer matches and SpamAssassin reports `DKIM_INVALID`, even though Microsoft's own `Authentication-Results` (taken
  before the rewrite) says `dkim=pass`. An allowlist entry for a rewritten message therefore rests on SPF alone.

### Logs

- Container log: human-readable lines with progress (`120 done, 380 left`), a run summary after each
  run (counts per decision, the Junk folder count, a score histogram) and `ERROR` lines for failed refreshes.
- `decisions.jsonl` fields: `ts`, `message_id`, `internet_message_id`, `received`, `from_addr`, `subject`
  (cut to 120 characters), `score`, `rules`, `keep_rule_hit`, `relays_untrusted`, `first_untrusted_ip`,
  `auth_results_present`, `mode`, `decision` (`keep`, `would-delete`, `would-quarantine`, `quarantined`, `deleted`,
  `permanently-deleted` or `error`), `reason`, `action_error`, `ms_scl` (Microsoft's verdict, empty if the header is absent), `ai_score` and `ai_reason` (when the AI rated it), and `current_message_id` and `folder` (where the message
  is now: Graph gives a moved message a new id). Message bodies, attachments and tokens are never logged.

## Not built

Alerting beyond log lines and the health check, a sign-in for the review page, moving wanted bulk mail out of Junk (only flagged messages move), Razor/Pyzor/DCC
and URL-reputation checks, a separate spamd container, sharing learned data between two instances, and scheduled rebuilds. Only SpamAssassin's
built-in DNS blocklists and URL lists run, when `/config/local.cf` and DNS allow them.

## Development

```bash
pip install -r requirements-dev.txt
ruff check .
pytest
```

The unit tests mock Graph and SpamAssassin and use synthetic `.eml` fixtures only; the review page is tested over real HTTP. CI also builds the image and runs
`tests/image/test-image.sh` against it. The image is `linux/amd64` only.

## Releasing

Releases are cut by the code session only: bump `VERSION` in a reviewed PR
and merge it; the release workflow waits for approval in the `release` Environment, then builds and pushes the image and creates the tag and
GitHub release. Changes reach `main` only through pull requests. Steps: [runbook](docs/RUNBOOK.md#9-release-process-maintainers).

## License

MIT, see [LICENSE](LICENSE). Third-party components are listed in [ATTRIBUTION.md](ATTRIBUTION.md).
