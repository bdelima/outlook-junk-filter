"""Sign-in with MSAL: one public-client app and one token cache file (/state/tokens/token.json)."""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import msal

AUTHORITY = "https://login.microsoftonline.com/consumers"
# MailboxSettings.ReadWrite is only for creating the Outlook categories the category hooks use
SCOPES = ["Mail.ReadWrite", "MailboxSettings.ReadWrite"]
HEALTH_EVERY = 60

# refresh failures that mean the refresh token is no longer valid (a new sign-in is needed)
_RENEW_ERRORS = ("invalid_grant", "interaction_required", "consent_required")


class AuthError(Exception):
    """No usable access token."""


@dataclass
class SilentResult:
    token: str | None = None
    needs_signin: bool = False
    error: str | None = None  # set when a refresh failed (log it)


class Auth:
    def __init__(self, client_id: str, state_dir: Path, log, touch_health=lambda: None):
        self._path = Path(state_dir) / "tokens" / "token.json"
        self._log = log
        self._touch = touch_health
        self._lock = threading.RLock()  # the job and the review page share this token cache
        self._cache = msal.SerializableTokenCache()
        if self._path.exists():
            try:
                self._cache.deserialize(self._path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                self._log.error("ERROR could not read the token cache: %s", exc)
        self._app = msal.PublicClientApplication(client_id, authority=AUTHORITY, token_cache=self._cache)

    def _save(self) -> None:
        if not self._cache.has_state_changed:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(self._cache.serialize())
        os.replace(tmp, self._path)
        os.chmod(self._path, 0o600)

    def silent(self) -> SilentResult:
        with self._lock:
            return self._silent()

    def _silent(self) -> SilentResult:
        accounts = self._app.get_accounts()
        if not accounts:
            return SilentResult(needs_signin=True)
        try:
            result = self._app.acquire_token_silent(SCOPES, account=accounts[0])
        except Exception as exc:  # network or MSAL failure: skip this run
            return SilentResult(error=f"{type(exc).__name__}: {exc}")
        self._save()
        if result and "access_token" in result:
            return SilentResult(token=result["access_token"])
        result = result or {}
        reason = result.get("error_description") or result.get("error") or "no token returned"
        return SilentResult(
            needs_signin=result.get("error") in _RENEW_ERRORS,
            error=str(reason).splitlines()[0][:300],
        )

    def account_name(self) -> str:
        """The signed-in mailbox address from the sign-in record (no extra Microsoft permission needed)."""
        with self._lock:
            accounts = self._app.get_accounts()
        return str(accounts[0].get("username") or "") if accounts else ""

    def access_token(self) -> str:
        """For the Graph client: a fresh access token, refreshed silently when needed."""
        result = self.silent()
        if result.token:
            return result.token
        raise AuthError(result.error or "sign-in needed")

    def sign_in(self) -> str:
        """Run the device code flow once. Returns "ok", "expired" (code ran out) or "failed" (could not start)."""
        flow = self._app.initiate_device_flow(scopes=SCOPES)
        if "user_code" not in flow:
            self._log.error("ERROR could not start sign-in: %s", flow.get("error_description", flow))
            return "failed"
        self._log.warning(
            "SIGN-IN NEEDED: open %s and enter code %s",
            flow["verification_uri"],
            flow["user_code"],
        )
        last = [time.monotonic()]

        def keep_alive(_flow) -> bool:
            if time.monotonic() - last[0] >= HEALTH_EVERY:
                self._touch()
                last[0] = time.monotonic()
            return False

        self._touch()
        result = self._app.acquire_token_by_device_flow(flow, exit_condition=keep_alive)
        if result and "access_token" in result:
            self._save()
            self._log.info("Signed in")
            return "ok"
        self._log.warning("The sign-in code expired or was refused; a fresh code follows")
        return "expired"
