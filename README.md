# outlook-junk-filter

Scores the Junk folder of one personal Outlook mailbox with SpamAssassin and deletes obvious phishing.

One long-lived container reads the **Junk folder only** of one personal Microsoft mailbox (a `live.com` or
`outlook.com` address) through Microsoft Graph, scores each message with SpamAssassin, and, in `act` mode, deletes messages
at or above a score you choose. Wanted bulk mail stays where it is. The Inbox and every other folder are never read
or changed. The only inbound traffic is the [review page](#the-review-page) (one port, no login: put it behind a reverse proxy).

One container instance runs exactly one mailbox. It starts in `report-only` mode, where no mail is changed. Nothing is
deleted until you have reviewed the scores and switched to `act`.

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
| `OJF_DELETE_THRESHOLD` | when a mode is `act`, unless `OJF_QUARANTINE_THRESHOLD` is set | none | SpamAssassin score, as a decimal, at or above which a message is deleted. |
| `OJF_QUARANTINE_ENABLED` | no | `on` | `on` or `off`. Turns the quarantine feature on or off. |
| `OJF_QUARANTINE_FOLDER` | no | `Quarantined` | Name of the top-level folder flagged messages are moved to. Any name works, except a system folder such as Junk or Inbox. Created if missing; an existing folder of that name is reused. |
| `OJF_QUARANTINE_THRESHOLD` | no | none | Score at or above which a message is moved to the folder. Must be lower than `OJF_DELETE_THRESHOLD` when both are set. With only this set, nothing is deleted at score time. |
| `OJF_QUARANTINE_RETENTION_DAYS` | no | `7` | Days a message stays in the folder before the job deletes it. The age counts from the message's received time, so one already older than this is deleted on the next pass. |
| `OJF_UI_PORT` | no | `60888` | Port of the review page (the stack publishes it). |
| `OJF_UI_CSRF` | no | `off` | `on` makes the review page's actions require a per-process form token and refuse a request whose `Origin` is not the page's own host (the proxy must then pass `Host` through unchanged). Off by default: the page has no login and is meant for a private LAN. |
| `OJF_KEEP_RULE_PATTERN` | no | `^USER_IN_.*(WHITE\|WELCOME)LIST` | Regular expression. A message that fired any matching rule is always kept. |
| `TZ` | no | `UTC` | Timezone of log timestamps. |

An empty value counts as unset. An unrecognized mode is treated as `report-only` and logged as an error. A mode of
`act` makes the job behave as `report-only`, and logs an error on every run, when `OJF_DELETE_MODE` is missing or
invalid, when neither `OJF_DELETE_THRESHOLD` nor `OJF_QUARANTINE_THRESHOLD` is set (a quarantine threshold counts only
while quarantine is on), when the quarantine threshold is not lower than the delete threshold, or when
`OJF_QUARANTINE_FOLDER` names the Junk folder or another system folder.

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
| `/state/logs/decisions.jsonl` | One JSON object per message, rotated by size (10 files of 10 MB) |
| `/state/logs/actions.jsonl` | What was done from the review page: time, action, message id, target (never a body) |
| `/state/restored.json` | Messages restored from the review page; the job keeps them (reason `restored`) |
| `/state/health` | Touched by the job; the container health check reads it |

The container runs as uid/gid 1000 with no home directory and works with a read-only root filesystem (only `/state`
and `/config` are written; the job itself never writes to `/config`, only the review page's allowlist action does). It needs outbound HTTPS to `graph.microsoft.com` and `login.microsoftonline.com`, plus DNS.

## The review page

A web page served by the job's own process (same container, same port you publish with `OJF_UI_PORT`, default 60888). It
shows the decisions the job logged and lets you act on them. There is **no login**: reach it through Nginx Proxy Manager
or keep the port on a trusted network. It reads only `/state/logs/`, makes no external requests, and escapes everything it
shows, because senders and subjects come from phishing mail.

- **Decisions:** time, sender, subject, score, decision and reason, 100 rows per page, newest first. Filters for
  decision, date range and score range; text search on sender and subject; sort by time or score; CSV export of
  the filtered table (cells that begin with `=`, `+`, `-` or `@` are prefixed so a spreadsheet does not run them).
- **Message detail:** every field of the log record, with the fired rules by name. The log holds no bodies, so to read a
  message open it in Outlook (in Junk or the quarantine folder).
- **Summary:** counts per decision per day, and a score histogram. A header shows how many records are
  loaded, the oldest and newest timestamps and the age of the last record. New records appear on reload.
- **What-if:** type a quarantine and a delete threshold; the page counts how many logged messages would be kept,
  quarantined or deleted, using the logged scores and allowlist hits. It saves nothing, and the scores do not reflect
  Bayes learning since.
- **Actions** (each on one message, with a confirmation click, each recorded in `actions.jsonl`):
  - **Teach Bayes** as spam or not spam: the job fetches the message from Outlook again and runs `sa-learn` against the
    Bayes database in `/state`. Works for any message still in Outlook (Junk, the quarantine folder or Deleted Items).
    Teach wanted bulk such as Reddit and Substack as *not spam*. A permanently deleted message is gone, so that row is
    view-only.
  - **Allowlist** a sender address or domain: adds a `whitelist_auth` line to `/config/ojf-allowlist.cf`, so it counts
    only when SPF or DKIM passes.
  - **Restore** a message from the quarantine folder or Deleted Items to Junk or the Inbox. The job then keeps it and
    never rescores it.
- **Auto-learn:** SpamAssassin's own `bayes_auto_learn` is on by default (`config/local.cf.example` carries the line, and
  `0` turns it off). Manual teaching works either way and corrects what auto-learn gets wrong.

## Roll it out safely

1. Leave `OJF_MODE` unset (`report-only`). The first run walks the whole Junk folder and logs a score and a would-delete
   verdict for each message; nothing is changed.
2. **Tune it.** Read the per-message log. Confirm Microsoft's `Received` headers record the connecting IP, that
   `auth_results_present` is true, and that `first_untrusted_ip` is the real sender's. Then set `trusted_networks` in
   `local.cf` to the Microsoft hops that appear in your own samples (set only `trusted_networks`; `internal_networks`
   defaults to it). Build the allowlist from sender domains that pass DKIM and carry `List-Unsubscribe`.
3. Choose the threshold where almost nothing wanted lies above it. SpamAssassin's default spam mark is 5.0, but its
   documentation advises deleting automatically only at exceptionally high scores such as 15.0 or higher.
4. Set `OJF_DELETE_MODE` (start with `deleted-items`) and a threshold: `OJF_QUARANTINE_THRESHOLD` to start by moving
   flagged mail to the quarantine folder (the gentlest way to build confidence), and later `OJF_DELETE_THRESHOLD`.
   Set `OJF_MODE=act`, update the stack, and check the quarantine folder by hand.

To bring in newer SpamAssassin rules, update the stack with force-rebuild: rules are fixed when the image is built.

### Logs

- Container log: human-readable lines with progress (`120 done, 380 left`), a run summary after each
  run (counts per decision, the Junk folder count, a score histogram) and `ERROR` lines for failed refreshes.
- `decisions.jsonl` fields: `ts`, `message_id`, `internet_message_id`, `received`, `from_addr`, `subject`
  (cut to 120 characters), `score`, `rules`, `keep_rule_hit`, `relays_untrusted`, `first_untrusted_ip`,
  `auth_results_present`, `mode`, `decision` (`keep`, `would-delete`, `would-quarantine`, `quarantined`, `deleted`,
  `permanently-deleted` or `error`), `reason`, `action_error`, and `current_message_id` and `folder` (where the message
  is now: Graph gives a moved message a new id). Message bodies, attachments and tokens are never logged.

## Not in v1

Alerting beyond log lines and the health check, a sign-in for the review page, moving wanted bulk mail out of Junk (only flagged messages move), Razor/Pyzor/DCC
and URL-reputation or LLM checks, a separate spamd container, and scheduled rebuilds are not built. Only SpamAssassin's
built-in DNS blocklists and URL lists run, when `/config/local.cf` and DNS allow them.

## Development

```bash
pip install -r requirements-dev.txt
ruff check .
pytest
```

The unit tests mock Graph and SpamAssassin and use synthetic `.eml` fixtures only. CI also builds the image and runs
`tests/image/test-image.sh` against it. The image is `linux/amd64` only.

## Releasing

Releases are cut by the code session only: bump `VERSION` in a reviewed PR
and merge it; the release workflow does the rest. Changes reach `main` only
through pull requests.

## License

MIT, see [LICENSE](LICENSE). Third-party components are listed in [ATTRIBUTION.md](ATTRIBUTION.md).
