"""Gmail as a mail source, over plain HTTPS.

Two separate Google permissions:
- read (gmail.metadata): headers and labels only, enforced by Google. The only sign-in that's saved.
- write (gmail.modify): search and Trash, asked for when you click Clean up. Google issues no refresh
  token for it, the app keeps it in memory for that one job, and it expires within an hour.
"""

import ipaddress
import logging
import os
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from urllib.parse import urlencode, urlsplit

import httpx

from app.mail import Message

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
API = "https://gmail.googleapis.com/gmail/v1/users/me"
READ_SCOPE = "https://www.googleapis.com/auth/gmail.metadata"
WRITE_SCOPE = "https://www.googleapis.com/auth/gmail.modify"
HEADERS = ["From", "Subject", "List-Unsubscribe", "List-Unsubscribe-Post"]
CATEGORIES = ["CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "CATEGORY_UPDATES", "CATEGORY_FORUMS", "CATEGORY_PERSONAL"]
NOT_INCOMING = {"SENT", "DRAFT", "CHAT"}
# Gmail reports per-user rate limits as 403 with these reasons, as well as 429; both mean wait and retry.
RATE_LIMITED = {"rateLimitExceeded", "userRateLimitExceeded"}
# Gmail's per-mailbox budget is 6,000 quota units a minute; staying at 90% avoids being told to wait.
UNITS_PER_MINUTE = 5400
# What each call costs against that budget (developers.google.com/workspace/gmail/api/reference/quota).
GET_COST, LIST_COST, MODIFY_COST = 20, 5, 50
PARALLEL_FETCHES = 3  # enough in flight to use the budget; Gmail also caps concurrent requests per mailbox
MAX_WAIT = 30  # seconds, when Gmail does ask us to slow down

log = logging.getLogger("recur-audit")

transport: httpx.BaseTransport | None = None  # tests swap in a fake Google


class SignInExpired(Exception):
    """Google no longer accepts the stored sign-in; the person has to connect again."""


class GoogleError(Exception):
    """Google refused a request; the message is Google's own explanation."""


class Pacer:
    """Spends a per-minute quota budget evenly across threads, so Gmail never has to say slow down."""

    def __init__(self, units_per_minute: float):
        self.seconds_per_unit = 60 / units_per_minute
        self.next_slot = time.monotonic()
        self.lock = threading.Lock()

    def wait(self, cost: int) -> None:
        with self.lock:
            slot = max(self.next_slot, time.monotonic())
            self.next_slot = slot + cost * self.seconds_per_unit
        time.sleep(max(0.0, slot - time.monotonic()))


def http() -> httpx.Client:
    return httpx.Client(timeout=30, transport=transport)


def configured() -> bool:
    return bool(os.environ.get("GOOGLE_CLIENT_ID") and os.environ.get("GOOGLE_CLIENT_SECRET"))


def sign_in_url(state: str, redirect_uri: str, write: bool = False, account: str | None = None) -> str:
    params = {
        "client_id": os.environ["GOOGLE_CLIENT_ID"], "redirect_uri": redirect_uri, "response_type": "code",
        "scope": WRITE_SCOPE if write else READ_SCOPE, "state": state, "prompt": "consent",
        # Only the read sign-in gets a refresh token; "online" write access can't outlive the hour.
        "access_type": "online" if write else "offline",
    }
    if account:
        params["login_hint"] = account
    return AUTH_URL + "?" + urlencode(params)


def finish_sign_in(code: str, redirect_uri: str) -> tuple[str, dict]:
    """Trade the code Google sent back for (mailbox address, tokens)."""
    with http() as client:
        tokens = _token(client, {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri})
        profile = client.get(f"{API}/profile", headers=_auth(tokens["access_token"]))
        profile.raise_for_status()
    return profile.json()["emailAddress"], tokens


def read_access(client: httpx.Client, refresh_token: str) -> str:
    return _token(client, {"grant_type": "refresh_token", "refresh_token": refresh_token})["access_token"]


def revoke(refresh_token: str) -> None:
    """Google revokes everything this app was granted, so only use it to disconnect."""
    with http() as client:
        client.post(REVOKE_URL, data={"token": refresh_token})


class Gmail:
    def __init__(self, client: httpx.Client, access_token: str):
        self.client = client
        self.headers = _auth(access_token)
        self.pacer = Pacer(UNITS_PER_MINUTE)

    def message(self, message_id: str) -> Message | None:
        """One message's headers, or None for mail you sent, drafts and chats."""
        body = self._call("GET", f"{API}/messages/{message_id}", GET_COST, params=[
            ("format", "metadata"), *[("metadataHeaders", h) for h in HEADERS],
            # Partial response: without this Google also sends a snippet of the message text.
            ("fields", "labelIds,internalDate,payload/headers"),
        ])
        labels = body.get("labelIds", [])
        if NOT_INCOMING & set(labels):
            return None
        headers = {h["name"].lower(): h["value"] for h in body.get("payload", {}).get("headers", [])}
        category = next((c.removeprefix("CATEGORY_") for c in CATEGORIES if c in labels), "PERSONAL")
        received = datetime.fromtimestamp(int(body["internalDate"]) / 1000, timezone.utc).date()
        return Message(headers.get("from", ""), headers.get("subject", ""), received, category,
                       headers.get("list-unsubscribe", ""), headers.get("list-unsubscribe-post", ""))

    def scan(self, since: date, progress) -> list[Message]:
        """Headers of incoming mail received since `since`. The read scope can't search, so this pages
        through the mailbox newest first and stops at the first page with nothing recent on it."""
        messages, checked, page = [], 0, None
        with ThreadPoolExecutor(max_workers=PARALLEL_FETCHES) as pool:
            while True:
                body = self._call("GET", f"{API}/messages", LIST_COST, params={"maxResults": 500, **({"pageToken": page} if page else {})})
                incoming = []
                for message in pool.map(self.message, [m["id"] for m in body.get("messages", [])]):
                    checked += 1
                    progress(checked, 0)
                    if message:
                        incoming.append(message)
                recent = [m for m in incoming if m.received >= since]
                messages += recent
                page = body.get("nextPageToken")
                if not page or (incoming and not recent):
                    return messages

    def move_from(self, address: str, to: str) -> int:
        """Move every message from this sender to TRASH (Gmail deletes it after 30 days) or SPAM (also teaches
        Gmail's filter). Needs write access. Returns how many messages moved."""
        ids, page = [], None
        while True:
            params = {"q": f'from:"{address}"', "maxResults": 500, **({"pageToken": page} if page else {})}
            body = self._call("GET", f"{API}/messages", LIST_COST, params=params)
            ids += [m["id"] for m in body.get("messages", [])]
            page = body.get("nextPageToken")
            if not page:
                break
        for start in range(0, len(ids), 1000):
            self._call("POST", f"{API}/messages/batchModify", MODIFY_COST,
                       json={"ids": ids[start:start + 1000], "addLabelIds": [to], "removeLabelIds": ["INBOX"]})
        return len(ids)

    def _call(self, method, url, cost: int, **kwargs) -> dict:
        for attempt in range(8):
            self.pacer.wait(cost)
            response = self.client.request(method, url, headers=self.headers, **kwargs)
            if not _worth_retrying(response):
                break
            wait = min(2 ** attempt, MAX_WAIT)  # rate limited or a hiccup at Google: back off and retry
            log.warning("Gmail answered %s (%s); retrying in %ss", response.status_code, _explanation(response), wait)
            time.sleep(wait)
        if response.is_error:
            raise GoogleError(f"{_explanation(response)} ({method} {url.removeprefix(API + '/')})")
        return response.json() if response.content else {}


def one_click_unsubscribe(url: str) -> bool:
    """RFC 8058 one-click unsubscribe. The URL comes from an email, so only public HTTPS hosts are allowed:
    a crafted link must not reach anything on the server's own network or the tailnet."""
    if not is_public_https(url):
        return False
    try:
        with http() as client:
            response = client.post(url, data={"List-Unsubscribe": "One-Click"}, follow_redirects=False)
        return response.status_code < 400
    except httpx.HTTPError:
        return False


def is_public_https(url: str) -> bool:
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        return False
    try:
        addresses = resolve(parts.hostname)
        return bool(addresses) and all(ipaddress.ip_address(a).is_global for a in addresses)
    except (OSError, ValueError):
        return False


def resolve(host: str) -> list[str]:
    return [info[4][0] for info in socket.getaddrinfo(host, 443)]


def _token(client: httpx.Client, data: dict) -> dict:
    response = client.post(TOKEN_URL, data={
        "client_id": os.environ["GOOGLE_CLIENT_ID"], "client_secret": os.environ["GOOGLE_CLIENT_SECRET"], **data})
    if response.status_code == 400 and response.json().get("error") == "invalid_grant":
        raise SignInExpired
    if response.is_error:
        raise GoogleError(_explanation(response))
    return response.json()


def _auth(access_token: str) -> dict:
    return {"Authorization": f"Bearer {access_token}"}


def _worth_retrying(response: httpx.Response) -> bool:
    if response.status_code in (429, 500, 502, 503):
        return True
    return response.status_code == 403 and bool(RATE_LIMITED & {e.get("reason") for e in _error(response).get("errors", [])})


def _explanation(response: httpx.Response) -> str:
    error = _error(response)
    return error.get("message") or error.get("error_description") or f"HTTP {response.status_code}"


def _error(response: httpx.Response) -> dict:
    """Google's error details: {"error": {...}} from APIs, {"error": "...", "error_description": ...} from sign-in."""
    try:
        body = response.json()
    except ValueError:
        return {}
    return body["error"] if isinstance(body.get("error"), dict) else body
