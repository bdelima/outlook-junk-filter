# Security Policy

Please report vulnerabilities privately through GitHub's
[private vulnerability reporting](https://github.com/bdelima/outlook-junk-filter/security/advisories/new)
rather than opening a public issue. I'll respond as soon as I can.

## Security model (for deployers)

- **The review page has no login.** Anyone who can reach its port can see senders and subjects, teach the filter, restore or
  delete messages, change the allowlist and delete log rows. Keep the port on a trusted network or behind an authenticating
  reverse proxy. `OJF_UI_CSRF=on` adds a per-process form token and an Origin check against cross-site requests; it is not a login.
- **`GET /api/stats` is part of that page** and returns counts only (no senders, subjects or message ids).
- **The token file is a key to the mailbox.** `/state/tokens/token.json` (mode 600) grants `Mail.ReadWrite` and `MailboxSettings.ReadWrite` to the signed-in
  mailbox. Back it up like a secret, never commit it, and revoke the app's access in the Microsoft account if it leaks.
- **Least privilege:** the app asks for the delegated `Mail.ReadWrite` and `MailboxSettings.ReadWrite` permissions only (the second creates the
  Outlook categories described in the README). It reads the Junk folder and acts on Junk, the quarantine folder and Deleted
  Items. The Inbox is never read or changed. A category applied in Outlook carries the same trust as the open mailbox. The container runs as uid 1000 on a read-only root filesystem and writes only
  `/state` and `/config`.
- **Untrusted input:** everything shown on the page comes from mail an attacker may have written and is escaped; the page sends a
  Content-Security-Policy that allows only its own scripts, styles and images, and makes no external requests. The optional AI
  server receives the sender, subject and the first 1,500 characters of text, and its reply is used only as a number.
- **The AI server has no authentication.** Keep it on a trusted network.
