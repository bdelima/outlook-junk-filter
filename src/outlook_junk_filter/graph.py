"""A small Microsoft Graph client: only the calls the spec lists, all under Mail.ReadWrite."""

from __future__ import annotations

import time
from urllib.parse import quote

import requests

BASE = "https://graph.microsoft.com/v1.0"
DELTA_SELECT = "id,receivedDateTime,from,subject,internetMessageId"
DELTA_URL = f"{BASE}/me/mailFolders/junkemail/messages/delta?$select={DELTA_SELECT}&$top=50"
MAX_RETRIES = 6
MAX_WAIT = 300
TIMEOUT = 60


class GraphError(Exception):
    """A Graph call failed with an HTTP status."""

    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status


class NetworkError(Exception):
    """The network failed; the account is skipped for this run."""


class DeltaGone(Exception):
    """Graph rejected the saved delta link (HTTP 410)."""


class GraphClient:
    def __init__(self, token_provider, session=None, sleep=time.sleep, log=None):
        self._token = token_provider
        self._session = session or requests.Session()
        self._sleep = sleep
        self._log = log

    def _request(self, method: str, url: str, *, json=None, expect=(200,)):
        for attempt in range(MAX_RETRIES + 1):
            headers = {"Authorization": f"Bearer {self._token()}"}
            try:
                resp = self._session.request(method, url, headers=headers, json=json, timeout=TIMEOUT)
            except requests.RequestException as exc:
                raise NetworkError(f"{type(exc).__name__}: {exc}") from exc
            if resp.status_code in (429, 503):
                if attempt == MAX_RETRIES:
                    raise GraphError(resp.status_code, "still throttled after retrying")
                try:
                    wait = int(float(resp.headers.get("Retry-After", "")))
                except ValueError:
                    wait = 30
                wait = max(1, min(wait, MAX_WAIT))
                if self._log:
                    self._log.warning("Graph answered %s; waiting %ss as told (Retry-After)", resp.status_code, wait)
                self._sleep(wait)
                continue
            if resp.status_code == 410 and "/delta" in url:
                raise DeltaGone()
            if resp.status_code not in expect:
                raise GraphError(resp.status_code, (resp.text or "")[:200])
            return resp
        raise GraphError(0, "unreachable")  # pragma: no cover

    def folder(self) -> dict:
        url = f"{BASE}/me/mailFolders/junkemail?$select=displayName,totalItemCount"
        return self._request("GET", url).json()

    def delta(self, saved_link: str | None) -> tuple:
        """Walk the delta query. Returns (messages, new_delta_link).

        A saved link that Graph rejects with 410 is discarded and the walk restarts from scratch.
        """
        try:
            return self._walk(saved_link or DELTA_URL)
        except DeltaGone:
            if self._log:
                self._log.warning("Graph rejected the saved delta link (410); walking the folder from scratch")
            try:
                return self._walk(DELTA_URL)
            except DeltaGone as exc:
                raise GraphError(410, "Graph rejected a fresh delta query") from exc

    def _walk(self, url: str) -> tuple:
        messages = []
        while True:
            data = self._request("GET", url).json()
            for item in data.get("value", []):
                if "@removed" in item:
                    continue
                messages.append(item)
            if "@odata.nextLink" in data:
                url = data["@odata.nextLink"]
                continue
            link = data.get("@odata.deltaLink")
            if not link:
                raise GraphError(200, "the delta query ended without a deltaLink")
            return messages, link

    def raw(self, message_id: str) -> bytes:
        return self._request("GET", f"{BASE}/me/messages/{quote(message_id, safe='')}/$value").content

    def move(self, message_id: str, destination: str) -> str:
        """Move a message to a folder (a folder id or a well-known name). Returns the moved message's new id."""
        resp = self._request(
            "POST",
            f"{BASE}/me/messages/{quote(message_id, safe='')}/move",
            json={"destinationId": destination},
            expect=(200, 201),
        )
        try:
            return (resp.json() or {}).get("id") or ""
        except ValueError:
            return ""

    def move_to_deleted_items(self, message_id: str) -> str:
        return self.move(message_id, "deleteditems")

    def find_folder(self, name: str) -> dict | None:
        """A top-level folder of this name, or None."""
        odata_name = "'" + name.replace("'", "''") + "'"
        flt = quote("displayName eq " + odata_name, safe="")
        url = f"{BASE}/me/mailFolders?$filter={flt}&$select=id,displayName"
        for item in self._request("GET", url).json().get("value", []):
            if (item.get("displayName") or "").casefold() == name.casefold():
                return item
        return None

    def create_folder(self, name: str) -> dict:
        """Create a top-level folder."""
        return self._request("POST", f"{BASE}/me/mailFolders", json={"displayName": name}, expect=(200, 201)).json()

    def well_known_folder_id(self, name: str) -> str:
        """The id of a well-known folder such as junkemail, inbox or deleteditems."""
        return self._request("GET", f"{BASE}/me/mailFolders/{name}?$select=id").json().get("id") or ""

    def old_messages(self, folder_id: str, received_before: str) -> list:
        """Messages in a folder received before an ISO time (UTC, ending in Z)."""
        flt = quote(f"receivedDateTime lt {received_before}", safe="")
        url = (
            f"{BASE}/me/mailFolders/{quote(folder_id, safe='')}/messages?$filter={flt}"
            f"&$select={DELTA_SELECT}&$top=50"
        )
        out = []
        while url:
            data = self._request("GET", url).json()
            out.extend(data.get("value", []))
            url = data.get("@odata.nextLink")
        return out

    def permanent_delete(self, message_id: str) -> None:
        self._request("POST", f"{BASE}/me/messages/{quote(message_id, safe='')}/permanentDelete", expect=(204,))
