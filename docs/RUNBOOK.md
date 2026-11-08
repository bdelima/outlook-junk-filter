# SpamWatch runbook

How to deploy, operate, upgrade and troubleshoot SpamWatch (the `outlook-junk-filter` container). For how it works and why, see
[DESIGN.md](DESIGN.md). For every setting, see the [README](../README.md).

Commands that talk to Docker assume `sudo docker ...` on the host. Replace `HOST` with the host's name or address.

## At a glance

| | |
| --- | --- |
| What it does | Scores the Junk folder of one personal Outlook mailbox; quarantines or deletes high scorers; review page for people |
| Image | `bdelima/outlook-junk-filter:<version>` or `:latest` (amd64) |
| Runs as | uid/gid 1000, read-only root filesystem OK |
| Port | Review page on `OJF_UI_PORT` (default 60888), no login |
| Writes to | `/state` (token, logs, Bayes, status) and `/config` (allowlist file only) |
| Needs | HTTPS to `graph.microsoft.com` and `login.microsoftonline.com`, DNS; optional LAN access to an AI server |
| Safe by default | `report-only`: nothing in the mailbox changes until `OJF_MODE=act` |
| Health | Docker health check reads `/state/health` (healthy while it was touched within the larger of 3 intervals and 30 minutes) |

## 1. Deploy the first instance

### Prepare (once)

1. **App registration** (Microsoft's app registration portal): new registration, *Personal Microsoft accounts only*, no redirect
   URI, *Allow public client flows* = **Yes**, Microsoft Graph **delegated** permission `Mail.ReadWrite`. Copy the
   *Application (client) ID*. It is not a secret.
2. **Host folders** (owned by 1000:1000, mode 700):

   ```bash
   sudo install -d -m 700 -o 1000 -g 1000 /opt/outlook-junk-filter /opt/outlook-junk-filter/state /opt/outlook-junk-filter/config
   ```

3. **SpamAssassin config:** copy [`config/local.cf.example`](../config/local.cf.example) to
   `/opt/outlook-junk-filter/config/local.cf` and set ownership to 1000:1000. It is yours; the job never writes it.

### Create the stack

In Portainer, add a stack from [`docker-compose.example.yml`](../docker-compose.example.yml). Put the settings in Portainer's
**Environment variables** panel (not a `.env` file). A minimal first deployment:

| Variable | Value |
| --- | --- |
| `OJF_CLIENT_ID` | the client ID from step 1 |
| `OJF_MODE` | leave empty (report-only) |
| `OJF_INTERVAL_SECONDS` | `900` (or leave empty) |
| `OJF_LOG_RETENTION_DAYS` | `30` (or leave empty) |
| `TZ` | in the compose file (`America/New_York` in the example) |

### First sign-in

```bash
sudo docker logs outlook-junk-filter 2>&1 | grep "SIGN-IN NEEDED"
```

Open the printed URL, enter the code, sign in with the **mailbox's primary address** and approve. The container then logs
`Signed in`, scans, and the review page header shows the mailbox address. The token is saved in `/state/tokens` (mode 600).
If the code expires first, the job logs that and prints a fresh one.

### Check it works

```bash
sudo docker logs outlook-junk-filter 2>&1 | tail -30
```

Expect, in order: the starting line with the effective settings, `Review page on port 60888`,
`SpamAssassin config loads cleanly`, `Junk folder is 'Junk Email' and holds N messages (mode report-only)`, progress lines
(`N done, M left`), and a `run summary`. Then open `http://HOST:60888/`.

A warning like *the first delta walk returned X messages but the Junk folder holds Y* means some messages were not walked; see
[Troubleshooting](#6-troubleshooting).

## 2. Go live (report-only to act)

1. Let the first full pass finish in `report-only`. On the review page, read the Decisions list: score and plain-English reasons
   (hover a score), `would-delete` and `would-quarantine` rows, and the Summary page's score histogram and SCL table.
2. **Tune.** Teach Bayes: mark wanted bulk (newsletters, receipts) *not spam* and obvious spam *spam*; Bayes starts scoring after
   200 of each. Allowlist wanted senders (counts only when SPF or DKIM passes; for Safe Links-rewritten mail that means SPF).
   Use **What-if** to see how many messages different thresholds would quarantine or delete.
3. Confirm the trust path (see the README's *Roll it out safely*): `auth_results_present` is true and `first_untrusted_ip` is the
   real sender's. Set `trusted_networks` in `local.cf` only from your own samples.
4. Pick thresholds with almost nothing wanted above them. SpamAssassin's default spam mark is 5.0; its documentation advises
   automatic deletion only at much higher scores. Start by **quarantining** (gentlest), delete later. Thresholds apply to the
   total score shown in the log: SpamAssassin's score plus any Microsoft SCL points, shipped-rule points and AI points.
5. Set, in Portainer: `OJF_DELETE_MODE=deleted-items` (recoverable), `OJF_QUARANTINE_THRESHOLD`, optionally
   `OJF_DELETE_THRESHOLD` (higher), then `OJF_MODE=act`. Update the stack.
6. Check the quarantine folder in Outlook by hand for the first days. Wrongly flagged messages: **Restore** from the review page
   (the job then keeps them).

`OJF_DELETE_MODE=permanent` is not recoverable. Use it only when you have watched `deleted-items` long enough to trust it.

## 3. Upgrade and roll back

**Upgrade:** in Portainer, update the stack with *Re-pull image* (for `:latest`), or change the tag. Watch the log for the
starting line (the version is in it) and a clean first pass. The state and config folders are untouched.

**Newer SpamAssassin rules** arrive only with a new image (rules are fixed at build time).

**Roll back:** set the image to the previous tag (for example `bdelima/outlook-junk-filter:0.11.0`) and update the stack. State
written by a newer version is read by older ones, except the review-page additions (status and tombstone files), which an older
version ignores. Pin a tag instead of `:latest` if you want to control when upgrades happen.

After an upgrade that changes scoring (new rules, thresholds), recheck the quarantine folder for a day.

## 4. Operate

### Daily and weekly

- Glance at the Homepage tile or the review page: last scan time, quarantined count, errors.
- Skim *quarantined* and *deleted* rows for anything wanted. Restore and teach as needed.
- Look for `error` decisions and `ERROR` lines in the container log.

### Review page tasks

| Task | How |
| --- | --- |
| See why a message scored what it did | Hover the score; open the message for every rule and the AI reason |
| Teach Bayes | Message page or batch: *Teach spam* / *not spam* (the message must still be in Outlook) |
| Restore a wrongly flagged message | Message page or batch: *Restore to Junk / Inbox* (from quarantine or Deleted Items) |
| Allowlist a sender | *Allowlist sender address / domain*. Takes effect on the next scoring pass |
| Act on many rows | Tick rows, **Actions ▾**, choose, confirm; or filter first |
| Delete history rows | *Delete from log* (batch or single), or *Delete all N matching…* with a filter. Outlook is not touched |
| Export | *Export CSV* (follows the filters) |

Ticks reset when the page reloads, and a batch covers the current page (at most 200 rows).

### Logs and pruning

- Container log: `sudo docker logs outlook-junk-filter`. Useful filters:

  ```bash
  sudo docker logs outlook-junk-filter 2>&1 | grep -E "ERROR|WARNING"
  sudo docker logs outlook-junk-filter 2>&1 | grep -E "run summary|Pruned the logs|AI rated|SCL header"
  ```

- History is pruned daily (and at startup): rows older than `OJF_LOG_RETENTION_DAYS` (default 30; `0` keeps everything), rows you
  deleted, and old teach/restore/delete records. A line `Pruned the logs: ...` says what was removed. Allowlist records are kept.
- The on-disk log is bounded anyway: `decisions.jsonl` rotates at 10 MB × 10 files.

### AI scoring

Off until `OJF_AI_URL` is set. The AI server is the `ovms-npu` image on a host with an Intel NPU (see
[`npu-server/README.md`](../npu-server/README.md)); on the AI host use `sudo docker`, the model folder must be owned by the user
the container runs as, and the container needs `/dev/accel`.

Verify after setting `OJF_AI_URL` (use the host's IP if name resolution inside the container is not reliable):

```bash
sudo docker logs outlook-junk-filter 2>&1 | grep "AI scoring server"
```

`AI scoring server http://HOST:8000/v3: model OpenVINO/Qwen3-8B-int4-cw-ov is available` means it is wired up. After a scan that
had in-range messages: `AI rated N of M scored messages (score range 0 to 6.0, up to +4.0 points); K failed`.
Only messages scored inside the range (and not allowlisted) are sent. The first request after the AI server starts is slow.

### Homepage

`GET http://HOST:60888/api/stats` returns counts as JSON (no login; counts only). A Homepage entry:

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

Other fields: `healthy`, `mode`, `account`, `version`, `last_7_days`, `log_records`, and `today.errors` / `today.ai_rated`.
Results are cached for 30 seconds.

## 5. A second mailbox

One container per mailbox. Everything per-mailbox is separate: token, state, Bayes database, allowlist, review page.

1. Create folders for it:

   ```bash
   sudo install -d -m 700 -o 1000 -g 1000 /opt/outlook-junk-filter-2 /opt/outlook-junk-filter-2/state /opt/outlook-junk-filter-2/config
   sudo cp /opt/outlook-junk-filter/config/local.cf /opt/outlook-junk-filter-2/config/local.cf
   sudo chown 1000:1000 /opt/outlook-junk-filter-2/config/local.cf
   ```

2. Add a second service to the stack (same image, its own folders, port and mode):

   ```yaml
     outlook-junk-filter-2:
       image: bdelima/outlook-junk-filter:latest
       container_name: outlook-junk-filter-2
       restart: unless-stopped
       user: "1000:1000"
       environment:
         - TZ=America/New_York
         - OJF_CLIENT_ID=${OJF_CLIENT_ID}
         - OJF_INTERVAL_SECONDS=${OJF_INTERVAL_SECONDS}
         - OJF_MODE=${OJF2_MODE}
         - OJF_DELETE_MODE=${OJF_DELETE_MODE}
         - OJF_DELETE_THRESHOLD=${OJF_DELETE_THRESHOLD}
         - OJF_QUARANTINE_ENABLED=${OJF_QUARANTINE_ENABLED}
         - OJF_QUARANTINE_FOLDER=${OJF_QUARANTINE_FOLDER}
         - OJF_QUARANTINE_THRESHOLD=${OJF_QUARANTINE_THRESHOLD}
         - OJF_QUARANTINE_RETENTION_DAYS=${OJF_QUARANTINE_RETENTION_DAYS}
         - OJF_LOG_RETENTION_DAYS=${OJF_LOG_RETENTION_DAYS}
         - OJF_UI_PORT=${OJF2_UI_PORT:-60889}
         - OJF_UI_CSRF=${OJF_UI_CSRF}
         - OJF_MS_SCL_POINTS=${OJF_MS_SCL_POINTS}
         - OJF_AI_URL=${OJF_AI_URL}
         - OJF_AI_MODEL=${OJF_AI_MODEL}
         - OJF_AI_MIN_SCORE=${OJF_AI_MIN_SCORE}
         - OJF_AI_MAX_SCORE=${OJF_AI_MAX_SCORE}
         - OJF_AI_MAX_POINTS=${OJF_AI_MAX_POINTS}
         - OJF_AI_TIMEOUT_SECONDS=${OJF_AI_TIMEOUT_SECONDS}
         - OJF_KEEP_RULE_PATTERN=${OJF_KEEP_RULE_PATTERN}
       ports:
         - "${OJF2_UI_PORT:-60889}:${OJF2_UI_PORT:-60889}"
       volumes:
         - /opt/outlook-junk-filter-2/state:/state
         - /opt/outlook-junk-filter-2/config:/config
   ```

   Keep `OJF2_MODE` empty (report-only) until the owner has seen the results and agreed to `act`. If the first instance uses
   custom `dns:` or networks, give the second the same.
3. The same **client ID can be used for both mailboxes** (an app registration for personal accounts is not tied to one
   mailbox); each mailbox signs in separately and its token stays in its own `/state`. Its device-code prompt appears in `sudo docker logs outlook-junk-filter-2`.
4. Thresholds tuned on one mailbox may not suit the other. Tune each separately.
5. **Do not share the Bayes folder** between the two containers: concurrent writers are untested. Each mailbox learns its own.
6. The review page has no login. Tell the other mailbox's owner what it shows (senders and subjects) and who can reach it.

## 6. Troubleshooting

| Symptom | Where to look | Cause and fix |
| --- | --- | --- |
| Container is `unhealthy` | `docker inspect` health, container log | Health is stale: the job loop is stuck or sign-in is waiting. Look for `SIGN-IN NEEDED`. A first pass over a large folder keeps touching health; a hang does not |
| `SIGN-IN NEEDED: open ... enter code ...` | Container log | No valid token. Complete the sign-in; it repeats with a fresh code if the code expires. A refresh failure that invalidates the token starts a new sign-in by itself |
| `ERROR token refresh failed: ...` | Container log | Transient network problem (retries next loop) or a revoked or expired token (a sign-in follows) |
| `network failure, skipping this run` / `Graph call failed` | Container log | Microsoft unreachable or throttling; next loop retries. Check DNS and outbound HTTPS from the container |
| Only some Junk messages scored, or the warning *the first delta walk returned X messages but the Junk folder holds Y* | Container log | Paging problem or messages left the folder meanwhile. 0.3.1 fixed the 50-message cap. If it persists, tell the maintainer with the counts |
| `gave up on a message after 3 attempts` | Container log, `error` rows | The message could not be fetched or scored three times and stays in Junk. Open the row for the reason |
| `ERROR SpamAssassin config check failed: ...` | Container log | `/config/local.cf` has a syntax error. Nothing is scored and everything is kept until it lints. Fix the file |
| `... behaving as report-only` with `ERROR` text | Container log | A mode of `act` with a missing or invalid `OJF_DELETE_MODE`, thresholds, or a colliding quarantine folder name. The message names the setting |
| `ERROR OJF_...=... is not ...` at startup | Container log | An invalid value; the default was used. Fix it in Portainer |
| Scores look low for obvious spam | Review page: score bubble, SCL column | Check the SCL header is present (`Microsoft SCL header present on N of M` in the log), the shipped rules, Bayes state, and the AI range. Teach examples |
| Legit mail flagged | Message page | See the rules that fired; allowlist the sender; teach *not spam*; restore |
| `DKIM_INVALID` on mail Microsoft says passed | Message page rules | Safe Links rewrote the links after signing. Allowlist rests on SPF for those |
| `ERROR AI scoring server ... is not usable` | Container log | The AI server did not answer `/models`. Check `OJF_AI_URL` (host, port; a typo such as `::` is caught at startup), the AI host's container, firewall. AI is skipped until it answers |
| `AI scoring server failed (...); skipping AI scoring for the rest of this run` | Container log | The server went away mid-run. No harm done; retried next run |
| AI never runs | Log lines `AI rated ...` absent | `OJF_AI_URL` empty or invalid, or no message scored inside `[OJF_AI_MIN_SCORE, OJF_AI_MAX_SCORE)`, or all were allowlisted |
| AI server `ZE_RESULT_ERROR_INVALID_ARGUMENT` | AI host container log | The container's NPU driver does not match the host firmware. Use the `ovms-npu` image whose driver matches (see `npu-server/README.md`) |
| Review page will not load | Browser, container log | `Review page on port N` absent or an `ERROR the review page could not start on port N` line: the port is in use. Check the published port and the proxy |
| Actions fail with a write error | Response page | `/state` or `/config` is not writable by uid 1000. Fix ownership: `sudo chown -R 1000:1000 /opt/outlook-junk-filter/state` and the config folder |
| *Teach* says the message is gone | Response page | The message was permanently deleted or moved by hand; it cannot be taught |
| Header shows no mailbox address | Page header | The job has not signed in since the upgrade to 0.11.0 or later. It appears after the first successful token acquisition |
| Homepage widget empty | `curl http://HOST:60888/api/stats` | Wrong URL or port, or a proxy in the way. Fields are described under *Homepage* |
| Disk use grows | `du -sh /opt/outlook-junk-filter/state` | Lower `OJF_LOG_RETENTION_DAYS`; the Bayes database and token are small. Pruning runs at startup and daily |

If something looks wrong and you are unsure, set `OJF_MODE` back to empty (report-only) and update the stack: the job stops changing
the mailbox on its next pass.

## 7. Back up and restore

Back up `/opt/outlook-junk-filter/config` (your rules and allowlist) and, if you want to keep learning, `state/bayes`. The token
(`state/tokens/token.json`) is a key to the mailbox: back it up like a secret and never commit it; without it you simply sign in
again. Logs and `state/delta/state.json` are disposable, but a lost delta file means the whole Junk folder is walked and scored
again from scratch: in `act` mode that acts on everything still in Junk (messages you restored are kept because of
`state/restored.json`), and each message gets a second row in the log. Mail already quarantined or deleted is no longer in Junk,
so it is not touched. Deleting the delta file with the stack stopped is also how you force a full re-scan on purpose.

To restore: recreate the folders (step 1), copy the files back with ownership 1000:1000, start the stack.

## 8. Revoke access and decommission

1. Stop the stack. The job stops changing the mailbox.
2. Revoke the app's access in the Microsoft account's privacy page (apps and services with access to your data), or remove the
   app registration.
3. Delete `state/tokens/token.json` and the folders under `/opt/outlook-junk-filter*` if you no longer need them.

## 9. Release process (maintainers)

1. Change the code in a branch and open a PR. CI (ruff, pytest, image build and image tests) must pass.
2. Bump `VERSION` (and the docs) in the PR that ships the change. Merge it to `main`.
3. The release workflow starts and **waits for approval** in the `release` Environment. Approve it on the run page.
4. It builds `linux/amd64`, pushes `bdelima/outlook-junk-filter:<version>` and `:latest`, then creates the tag and the GitHub
   release. A version whose tag already exists is skipped; a forced re-run is needed to rebuild.
5. Check: the run is green, the tag and release exist, and Docker Hub shows both tags for amd64.
6. Update the stack on the host (section 3).

Merging several stacked PRs: merge the top PR with a merge commit so `VERSION` changes once and only one release runs. The
AI server image (`bdelima/ovms-npu`) releases the same way from `npu-server/VERSION`, with the tag `ovms-npu-<version>`.
