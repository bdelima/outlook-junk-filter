# outlook-junk-filter

Scores the Junk folder of two personal Outlook accounts with SpamAssassin and deletes obvious phishing.

One long-lived container reads the **Junk folder only** of two personal Microsoft accounts (`live.com` and
`outlook.com`) through Microsoft Graph, scores each message with SpamAssassin, and, in `act` mode, deletes messages
at or above a score you choose. Wanted bulk mail stays where it is. The Inbox and every other folder are never read
or changed. No web UI, no ports, no inbound traffic.

Every account starts in `report-only` mode, where no mail is changed. Nothing is deleted until you have reviewed the
scores and switched an account to `act`.

## How it works

- Each loop (every `OJF_INTERVAL_SECONDS`) handles the two accounts in turn: it walks the Junk folder's Graph delta
  query, fetches each new message as raw MIME, scores it with `spamassassin --siteconfigpath /config` (no spamd),
  decides, and logs.
- Decision, in order: a fired rule matches `OJF_KEEP_RULE_PATTERN` (allowlisted) means keep; a message that could not
  be scored means keep and retry (3 attempts, then give up and keep); a score at or above `OJF_DELETE_THRESHOLD`
  means delete (`would-delete` in report-only mode); anything else means keep.
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

Add a stack with [`docker-compose.example.yml`](docker-compose.example.yml). No ports are published and no network is
declared. Set the variables in Portainer's **Environment variables** panel, not in a `.env` file.

| Variable | Required | Default | Meaning |
| --- | --- | --- | --- |
| `OJF_CLIENT_ID` | yes | none | Application (client) ID of your app registration (not a secret). The process exits with a clear message if it is missing. |
| `OJF_INTERVAL_SECONDS` | no | `900` | Seconds between runs. Values below 60 are raised to 60 and logged. |
| `OJF_MODE_LIVE` | no | `report-only` | `report-only` or `act`, for the live.com account. |
| `OJF_MODE_OUTLOOK` | no | `report-only` | The same, for the outlook.com account. |
| `OJF_DELETE_MODE` | when a mode is `act` | none | `deleted-items` (recoverable in Outlook) or `permanent` (not recoverable). Neither is a default. |
| `OJF_DELETE_THRESHOLD` | when a mode is `act` | none | SpamAssassin score, as a decimal, at or above which a message is deleted. |
| `OJF_MAX_DELETES_PER_RUN` | no | no cap | If set, an account stops deleting after this many deletions in one run; the rest wait for the next run. |
| `OJF_KEEP_RULE_PATTERN` | no | `^USER_IN_.*(WHITE\|WELCOME)LIST` | Regular expression. A message that fired any matching rule is always kept. |
| `TZ` | no | `UTC` | Timezone of log timestamps. |

An empty value counts as unset. An unrecognized mode is treated as `report-only` and logged as an error. A mode of
`act` with a missing or invalid `OJF_DELETE_MODE` or `OJF_DELETE_THRESHOLD` makes that account behave as
`report-only` and logs an error on every run.

### First sign-in

On first start an account has no token, so the job logs a line like this and waits:

```text
SIGN-IN NEEDED live: open https://www.microsoft.com/link and enter code ABCD1234
```

Read it in Portainer's **Logs** view for the container, open the URL, enter the code, sign in with **that** account and
approve. Do this once per account. Tokens are saved under `/state/tokens` (mode 600) and refresh silently each run. If
a code expires first, the job logs that and issues a fresh one. An account waiting for sign-in never blocks the other.

### What lives where

| Path in the container | Holds |
| --- | --- |
| `/config/local.cf` | SpamAssassin configuration, owned by you (read-only to the job) |
| `/state/tokens/<account>.json` | MSAL token cache, one per account, mode 600. **These are keys to your mail: back them up like secrets, never commit them.** |
| `/state/delta/<account>.json` | Delta link, retry list, handled message ids (entries older than 90 days are dropped) |
| `/state/bayes/` | SpamAssassin's Bayes database (inactive in v1: no training is built) |
| `/state/logs/decisions.jsonl` | One JSON object per message, rotated by size (10 files of 10 MB) |
| `/state/health` | Touched by the job; the container health check reads it |

The container runs as uid/gid 1000 with no home directory and works with a read-only root filesystem (only `/state`
is written). It needs outbound HTTPS to `graph.microsoft.com` and `login.microsoftonline.com`, plus DNS.

## Roll it out safely

1. Leave both accounts in `report-only`. The first run walks the whole Junk folder and logs a score and a would-delete
   verdict for each message; nothing is changed.
2. **Tune it.** Read the per-message log. Confirm Microsoft's `Received` headers record the connecting IP, that
   `auth_results_present` is true, and that `first_untrusted_ip` is the real sender's. Then set `trusted_networks` in
   `local.cf` to the Microsoft hops that appear in your own samples (set only `trusted_networks`; `internal_networks`
   defaults to it). Build the allowlist from sender domains that pass DKIM and carry `List-Unsubscribe`.
3. Choose the threshold where almost nothing wanted lies above it. SpamAssassin's default spam mark is 5.0, but its
   documentation advises deleting automatically only at exceptionally high scores such as 15.0 or higher.
4. Set `OJF_DELETE_MODE` (start with `deleted-items`), `OJF_DELETE_THRESHOLD`, optionally `OJF_MAX_DELETES_PER_RUN`,
   switch **one** account to `act`, update the stack, and check that account's Deleted Items by hand.
5. Then enable the second account the same way.

To bring in newer SpamAssassin rules, update the stack with force-rebuild: rules are fixed when the image is built.

### Logs

- Container log: human-readable lines with progress per account (`live: 120 done, 380 left`), a run summary after each
  account (counts per decision, the Junk folder count, a score histogram) and `ERROR` lines for failed refreshes.
- `decisions.jsonl` fields: `ts`, `account`, `message_id`, `internet_message_id`, `received`, `from_addr`, `subject`
  (cut to 120 characters), `score`, `rules`, `keep_rule_hit`, `relays_untrusted`, `first_untrusted_ip`,
  `auth_results_present`, `mode`, `decision` (`keep`, `would-delete`, `deleted`, `permanently-deleted` or `error`),
  `reason` and `action_error`. Message bodies, attachments and tokens are never logged.

## Not in v1

Alerting beyond log lines and the health check, Bayes training, moving wanted bulk mail to another folder, Razor/Pyzor/DCC
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
