import json
import re
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from app import gmail, main

ME = {"Tailscale-User-Login": "alex@example.com"}
PARTNER = {"Tailscale-User-Login": "sam@example.com"}
HTMX = {"HX-Request": "true"}
DAY_MS = 86_400_000
RECENT = str(int(time.time() * 1000) - 30 * DAY_MS)
TWO_MONTHS_AGO = str(int(time.time() * 1000) - 60 * DAY_MS)
OLD = str(int(time.time() * 1000) - 800 * DAY_MS)

# id -> (From, Subject, labels, List-Unsubscribe, List-Unsubscribe-Post, received)
MAILBOX = {
    "m1": ("Shop <news@shop.example>", "Big sale", ["CATEGORY_PROMOTIONS"], "<https://shop.example/u>", "List-Unsubscribe=One-Click", RECENT),
    "m2": ("Shop <news@shop.example>", "Bigger sale", ["CATEGORY_PROMOTIONS"], "<https://shop.example/u>", "List-Unsubscribe=One-Click", RECENT),
    "m3": ("Bank <alerts@bank.example>", "Your statement is ready", ["CATEGORY_UPDATES"], "", "", RECENT),
    "m4": ("Junk <x@junk.example>", "WIN NOW", ["CATEGORY_PROMOTIONS"], "", "", RECENT),
    "m5": ("Junk <x@junk.example>", "WIN AGAIN", ["CATEGORY_PROMOTIONS"], "", "", RECENT),
    "m6": ("Junk <x@junk.example>", "LAST CHANCE", ["CATEGORY_PROMOTIONS"], "", "", RECENT),
    # Its unsubscribe link points inside the server's own network; the app must never call it.
    "m7": ("Sneaky <hi@sneaky.example>", "Deals", ["CATEGORY_PROMOTIONS"], "<https://internal.example/u>", "List-Unsubscribe=One-Click", RECENT),
    "m8": ("Old Friend <old@friend.example>", "from years ago", ["CATEGORY_PERSONAL"], "", "", OLD),
    "m9": ("Me <old@example.com>", "a reply I sent", ["SENT"], "", "", RECENT),
    "m10": ("Club <hello@club.example>", "Season update", ["CATEGORY_PERSONAL"], "", "", TWO_MONTHS_AGO),
    "m11": ("Cloudly <billing@cloudly.example>", "Your Cloudly plan renews on Dec 1", ["CATEGORY_UPDATES"], "", "", RECENT),
    "m12": ("Cloudly <billing@cloudly.example>", "Invoice #1042 from Cloudly", ["CATEGORY_UPDATES"], "", "", RECENT),
}
PUBLIC_HOSTS = {"shop.example": "93.184.216.34", "internal.example": "127.0.0.1"}
# What each sign-in code turns into, like Google: only the read sign-in comes with a refresh token.
CODES = {"read-code": {"access_token": "read-token", "refresh_token": "refresh"},
         "write-code": {"access_token": "write-token"},
         "other-account-code": {"access_token": "other-token"}}


class FakeGoogle:
    """Behaves like Gmail's API for the calls the app makes, including what each permission may do."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.trashed: list[str] = []
        self.spammed: list[str] = []
        self.unsubscribed: list[str] = []
        self.sign_in_valid = True
        self.gmail_errors: list[httpx.Response] = []  # returned, in order, by the next Gmail API calls

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url, path = str(request.url), request.url.path
        if url.startswith(gmail.API) and self.gmail_errors:
            return self.gmail_errors.pop(0)
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        if url.startswith(gmail.TOKEN_URL):
            form = parse_qs(request.content.decode())
            if form["grant_type"] == ["refresh_token"]:
                if not self.sign_in_valid:
                    return httpx.Response(400, json={"error": "invalid_grant"})
                return httpx.Response(200, json={"access_token": "read-token"})
            return httpx.Response(200, json=CODES[form["code"][0]])
        if url.startswith(gmail.REVOKE_URL):
            return httpx.Response(200)
        if path.endswith("/profile"):
            return httpx.Response(200, json={"emailAddress": "other@example.com" if token == "other-token" else "old@example.com"})
        if path.endswith("/messages"):
            query = request.url.params.get("q")
            if query is None:
                return httpx.Response(200, json={"messages": [{"id": i} for i in MAILBOX]})
            if token != "write-token":
                return httpx.Response(403, json={"error": "Metadata scope does not support 'q' parameter"})
            sender = re.fullmatch(r'from:"(.+)"', query).group(1)
            return httpx.Response(200, json={"messages": [{"id": i} for i, m in MAILBOX.items() if f"<{sender}>" in m[0]]})
        if path.endswith("/batchModify"):
            if token != "write-token":
                return httpx.Response(403, json={"error": "Insufficient Permission"})
            body = json.loads(request.content)
            assert body["removeLabelIds"] == ["INBOX"]
            {"TRASH": self.trashed, "SPAM": self.spammed}[body["addLabelIds"][0]].extend(body["ids"])
            return httpx.Response(204)
        if "/messages/" in path:
            assert request.url.params["format"] == "metadata"
            sender, subject, labels, unsubscribe, post, received = MAILBOX[path.rsplit("/", 1)[1]]
            headers = [{"name": "From", "value": sender}, {"name": "Subject", "value": subject}]
            if unsubscribe:
                headers += [{"name": "List-Unsubscribe", "value": unsubscribe},
                            {"name": "List-Unsubscribe-Post", "value": post}]
            return httpx.Response(200, json={"labelIds": ["INBOX", *labels], "internalDate": received,
                                             "payload": {"headers": headers}})
        if request.url.host in PUBLIC_HOSTS:
            self.unsubscribed.append(url)
            return httpx.Response(200)
        raise AssertionError(f"unexpected request {request.method} {url}")


@pytest.fixture
def google(monkeypatch):
    fake = FakeGoogle()
    monkeypatch.setattr(gmail, "transport", httpx.MockTransport(fake))
    monkeypatch.setattr(gmail, "resolve", lambda host: [PUBLIC_HOSTS[host]])
    monkeypatch.setattr(gmail, "UNITS_PER_MINUTE", 10_000_000)  # the real budget would make tests slow
    return fake


@pytest.fixture
def client(tmp_path, monkeypatch, google):
    monkeypatch.setenv("RECUR_AUDIT_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("RECUR_AUDIT_LOCAL_USER", raising=False)
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "client-secret")
    monkeypatch.setattr(main, "start_thread", lambda work: work())  # run background jobs inline
    main.JOBS.clear()
    return TestClient(main.app, base_url="http://localhost")


def query(url: str) -> dict:
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


def connect(client, headers=ME):
    state = query(client.get("/email/connect", headers=headers, follow_redirects=False).headers["location"])["state"]
    return client.get("/email/callback", params={"state": state, "code": "read-code"}, headers=headers, follow_redirects=False)


def clean(client, code="write-code"):
    """Click Clean up, approve on Google's side with `code`, come back."""
    google_url = client.post("/email/clean", headers={**ME, **HTMX}).headers["HX-Redirect"]
    response = client.get("/email/callback", params={"state": query(google_url)["state"], "code": code},
                          headers=ME, follow_redirects=False)
    return google_url, response


def scanned(client):
    connect(client)
    client.post("/email/scan", headers={**ME, **HTMX})


def section(page: str, title: str) -> str:
    """The HTML of one action's section on the email page."""
    return page.split(f"<h2>{title}")[1].split("</section>")[0]


def test_connecting_asks_only_for_read_only_headers_access(client):
    location = client.get("/email/connect", headers=ME, follow_redirects=False).headers["location"]
    assert query(location)["scope"] == "https://www.googleapis.com/auth/gmail.metadata"
    assert query(location)["access_type"] == "offline"


def test_clean_up_asks_for_one_time_write_access_on_the_same_account(client):
    scanned(client)
    google_url, _ = clean(client)
    assert query(google_url)["scope"] == "https://www.googleapis.com/auth/gmail.modify"
    assert query(google_url)["access_type"] == "online"  # Google issues no refresh token for it
    assert query(google_url)["login_hint"] == "old@example.com"


def test_a_sign_in_must_finish_with_the_state_it_started_with(client):
    assert client.get("/email/callback", params={"state": "made-up", "code": "x"}, headers=ME).status_code == 400
    state = query(client.get("/email/connect", headers=ME, follow_redirects=False).headers["location"])["state"]
    # Someone else can't finish your sign-in, and a state works only once.
    assert client.get("/email/callback", params={"state": state, "code": "read-code"}, headers=PARTNER).status_code == 400
    assert client.get("/email/callback", params={"state": state, "code": "read-code"}, headers=ME,
                      follow_redirects=False).status_code == 303
    assert client.get("/email/callback", params={"state": state, "code": "read-code"}, headers=ME).status_code == 400


def test_scan_groups_incoming_mail_from_the_last_year(client):
    scanned(client)
    page = client.get("/email", headers=ME).text
    assert "old@example.com" in page
    assert "news@shop.example" in section(page, "Unsubscribe &amp; trash")
    assert "alerts@bank.example" in section(page, "Move to new address")
    assert "x@junk.example" in section(page, "Spam")
    assert "hello@club.example" in page  # two months old: inside the default 3 months
    assert "old@friend.example" not in page  # years old
    assert "a reply I sent" not in page  # your own sent mail isn't a sender to clean up
    assert "Clean up 3 senders: unsubscribe &amp; trash 2, move 1 to Spam" in re.sub(r"<[^>]+>", "", page)


def test_only_headers_are_ever_requested(client, google):
    scanned(client)
    fetches = [r for r in google.requests if re.search(r"/messages/m\d+$", r.url.path)]
    assert len(fetches) == len(MAILBOX)
    for request in fetches:
        assert "snippet" not in request.url.params["fields"] and "body" not in request.url.params["fields"]


def test_clean_up_unsubscribes_and_trashes_only_what_was_chosen(client, google):
    scanned(client)
    client.post("/email/action", headers={**ME, **HTMX}, data={"address": "x@junk.example", "action": "keep"})
    clean(client)

    assert google.unsubscribed == ["https://shop.example/u"]  # never the link into the private network
    assert sorted(google.trashed) == ["m1", "m2", "m7"]  # not the bank, not the junk now marked keep
    page = " ".join(re.sub(r"<[^>]+>", " ", client.get("/email", headers=ME).text).split())
    assert "Unsubscribed · 2 emails trashed" in page
    assert "Unsubscribe yourself · 1 email trashed" in page


def test_write_access_is_never_saved(client, tmp_path):
    scanned(client)
    clean(client)
    stored = b"".join(f.read_bytes() for f in tmp_path.rglob("*") if f.is_file())
    assert b"write-token" not in stored
    assert b"refresh" in stored  # the read-only sign-in is what stays


def test_clean_up_refuses_approval_from_a_different_account(client, google):
    scanned(client)
    _, response = clean(client, code="other-account-code")
    assert response.status_code == 400
    assert google.trashed == [] and google.unsubscribed == []


def test_unsubscribe_links_must_be_public_https(google):
    gmail.resolve = lambda host: {"public.example": ["93.184.216.34"], "lan.example": ["192.168.1.5"],
                                  "tailnet.example": ["100.101.102.103"], "loop.example": ["::1"]}[host]
    assert gmail.is_public_https("https://public.example/u")
    assert not gmail.is_public_https("http://public.example/u")
    for host in ("lan.example", "tailnet.example", "loop.example"):
        assert not gmail.is_public_https(f"https://{host}/u")


def test_disconnecting_revokes_and_forgets_everything(client, google):
    scanned(client)
    client.post("/email/disconnect", headers={**ME, **HTMX})
    page = client.get("/email", headers=ME).text
    assert "Connect Gmail" in page and "news@shop.example" not in page
    assert any(str(r.url).startswith(gmail.REVOKE_URL) for r in google.requests)


def test_an_expired_sign_in_asks_to_connect_again(client, google):
    connect(client)
    google.sign_in_valid = False
    client.post("/email/scan", headers={**ME, **HTMX})
    page = client.get("/email", headers=ME).text
    assert "Connect again" in page and "Connect Gmail" in page


def test_each_person_has_their_own_mailbox(client):
    scanned(client)
    page = client.get("/email", headers=PARTNER).text
    assert "Connect Gmail" in page and "news@shop.example" not in page


def test_email_actions_require_htmx(client):
    connect(client)
    assert client.post("/email/scan", headers=ME).status_code == 403
    assert client.post("/email/clean", headers=ME).status_code == 403


def test_one_job_at_a_time(client, monkeypatch):
    connect(client)
    monkeypatch.setattr(main, "start_thread", lambda work: None)  # leave the scan running
    client.post("/email/scan", headers={**ME, **HTMX})
    assert client.post("/email/clean", headers={**ME, **HTMX}).status_code == 409


def google_error(status: int, reason: str, message: str) -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": status, "message": message, "errors": [{"reason": reason}]}})


def test_gmail_rate_limits_are_waited_out(client, google, monkeypatch):
    monkeypatch.setattr(gmail.time, "sleep", lambda seconds: None)
    connect(client)
    google.gmail_errors = [google_error(403, "userRateLimitExceeded", "User-rate limit exceeded"),
                           google_error(429, "rateLimitExceeded", "Too many concurrent requests for user")]
    client.post("/email/scan", headers={**ME, **HTMX})
    assert "news@shop.example" in client.get("/email", headers=ME).text


def test_other_google_errors_show_googles_reason(client, google, monkeypatch):
    connect(client)
    google.gmail_errors = [google_error(403, "accessNotConfigured", "Gmail API has not been used in project 123")]
    client.post("/email/scan", headers={**ME, **HTMX})
    assert "Gmail API has not been used in project 123" in client.get("/email", headers=ME).text


def test_scan_progress_counts_each_email(client, monkeypatch):
    seen = []
    real_run_job = main.run_job

    def recording_run_job(owner, kind, work):
        def wrapped(conn, progress):
            work(conn, lambda done, total: (seen.append(done), progress(done, total)))
        real_run_job(owner, kind, wrapped)

    monkeypatch.setattr(main, "run_job", recording_run_job)
    scanned(client)
    assert seen == list(range(1, len(MAILBOX) + 1))


def test_requests_are_paced_by_their_quota_cost():
    pacer = gmail.Pacer(units_per_minute=6000)  # 100 units a second
    start = time.monotonic()
    for _ in range(4):
        pacer.wait(cost=20)  # each email read costs 20 units, so 4 reads take about 0.6s
    assert 0.5 < time.monotonic() - start < 1.2


def test_scan_period_can_be_one_three_or_twelve_months(client):
    connect(client)
    client.post("/email/scan", headers={**ME, **HTMX}, data={"months": "1"})
    page = client.get("/email", headers=ME).text
    assert "news@shop.example" in page and "hello@club.example" not in page
    assert client.post("/email/scan", headers={**ME, **HTMX}, data={"months": "7"}).status_code == 400


def test_scans_saved_under_the_old_labels_are_converted(tmp_path):
    from app import db
    conn = db.connect(tmp_path / "old.db")
    for address, proposed, action in [("a@x.example", "move_new", "move_new"), ("b@x.example", "move_spam", "move_spam"),
                                      ("c@x.example", "trash", "trash"), ("d@x.example", "unsubscribe", "keep")]:
        conn.execute("""INSERT INTO mail_senders (owner, address, name, messages, last_seen, category, latest_subject,
                        one_click, proposed, action) VALUES ('me', ?, ?, 1, '2026-11-01', 'PROMOTIONS', '', 0, ?, ?)""",
                     (address, address, proposed, action))
    conn.commit()
    conn.close()
    conn = db.connect(tmp_path / "old.db")
    rows = {r["address"]: (r["proposed"], r["action"]) for r in db.mail_senders(conn, "me")}
    assert rows == {"a@x.example": ("move", "move"), "b@x.example": ("keep", "keep"),
                    "c@x.example": ("spam", "spam"), "d@x.example": ("unsubscribe", "keep")}


def test_spam_moves_mail_to_spam_and_only_unsubscribes_verified_senders(client, google):
    scanned(client)  # junk (no unsubscribe link) is suggested as spam
    client.post("/email/action", headers={**ME, **HTMX}, data={"address": "news@shop.example", "action": "spam"})
    clean(client)
    assert sorted(google.spammed) == ["m1", "m2", "m4", "m5", "m6"]  # shop + junk
    assert google.trashed == ["m7"]  # sneaky stays unsubscribe & trash
    assert google.unsubscribed == ["https://shop.example/u"]  # shop has one-click; junk has no link at all
    page = " ".join(re.sub(r"<[^>]+>", " ", client.get("/email", headers=ME).text).split())
    assert "Unsubscribed · 2 emails moved to Spam" in page
    assert "3 emails moved to Spam" in page


def test_move_label_follows_the_new_address_setting(client):
    scanned(client)
    assert client.post("/email/new-address", headers={**ME, **HTMX}, data={"new_address": "nope"}).status_code == 400
    client.post("/email/new-address", headers={**ME, **HTMX}, data={"new_address": "me@new.example"})
    page = client.get("/email", headers=ME).text
    assert "alerts@bank.example" in section(page, "Move to me@new.example")
    client.post("/email/new-address", headers={**ME, **HTMX}, data={"new_address": ""})
    assert "Move to new address" in client.get("/email", headers=ME).text


def test_ticking_done_moves_a_row_out_of_the_checklist(client, google):
    scanned(client)
    client.post("/email/done", headers={**ME, **HTMX}, data={"address": "alerts@bank.example", "done": "true"})
    page = client.get("/email", headers=ME).text
    assert "alerts@bank.example" not in section(page, "Move to new address")
    assert "alerts@bank.example" in page.split("<h2>Moved")[1]
    clean(client)
    assert "m3" not in google.trashed + google.spammed  # Clean up never touches move rows
    client.post("/email/done", headers={**ME, **HTMX}, data={"address": "alerts@bank.example", "done": "false"})
    assert "alerts@bank.example" in section(client.get("/email", headers=ME).text, "Move to new address")


def test_senders_billing_you_on_repeat_are_grouped_and_marked(client):
    scanned(client)
    page = client.get("/email", headers=ME).text
    charging = page.split("Charging you")[1].split("</section>")[0]
    assert "billing@cloudly.example" in charging  # a renewal and an invoice
    for address in ("news@shop.example", "hello@club.example", "alerts@bank.example"):
        assert address not in charging  # marketing, a person, a statement notice
    assert '<span class="dollar" title="Bills you on repeat">$</span>Cloudly' in page


def test_a_database_from_before_the_costs_flag_gets_the_column(tmp_path):
    import sqlite3
    from app import db
    old = sqlite3.connect(tmp_path / "old.db")
    old.execute("""CREATE TABLE mail_senders (owner TEXT NOT NULL, address TEXT NOT NULL, name TEXT NOT NULL,
                   messages INTEGER NOT NULL, last_seen TEXT NOT NULL, category TEXT NOT NULL, latest_subject TEXT NOT NULL,
                   unsubscribe_url TEXT, one_click INTEGER NOT NULL, proposed TEXT NOT NULL, action TEXT NOT NULL,
                   result TEXT, PRIMARY KEY (owner, address))""")
    old.execute("""INSERT INTO mail_senders VALUES ('me', 'a@x.example', 'A', 1, '2026-11-01', 'UPDATES',
                   'Your receipt', NULL, 0, 'keep', 'keep', NULL)""")
    old.commit()
    old.close()
    conn = db.connect(tmp_path / "old.db")
    assert db.mail_senders(conn, "me")[0]["costs_money"] == 0


def test_dollar_senders_come_first_in_each_section(client):
    scanned(client)
    client.post("/email/action", headers={**ME, **HTMX}, data={"address": "x@junk.example", "action": "move"})
    rows = section(client.get("/email", headers=ME).text, "Move to new address")
    assert rows.index("billing@cloudly.example") < rows.index("x@junk.example")  # 2 emails with $ before 3 without
