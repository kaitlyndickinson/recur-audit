import re

import pytest
from fastapi.testclient import TestClient

from app.main import app

ME = {"Tailscale-User-Login": "alex@example.com"}
PARTNER = {"Tailscale-User-Login": "sam@example.com"}
HTMX = {"HX-Request": "true"}


def monthly_csv(months: range, description="STREAMFLIX.COM 866-555-0100 CA", amount="-15.49") -> bytes:
    lines = ["Date,Description,Amount"] + [f"2026-{m:02d}-14,{description},{amount}" for m in months]
    return ("\n".join(lines) + "\n").encode()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("HOUSEHOLD_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("HOUSEHOLD_DEV_USER", raising=False)
    return TestClient(app)


def visible_text(html: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", html).split())


def upload(client, content: bytes, headers=ME, account="Card", name="export.csv"):
    return client.post("/import", headers={**headers, **HTMX},
                       files={"file": (name, content, "text/csv")}, data={"account": account})


def test_requests_without_tailscale_identity_are_refused(client):
    assert client.get("/").status_code == 403


def test_imported_subscription_shows_on_home(client):
    response = upload(client, monthly_csv(range(1, 7)))
    assert "6 new transactions" in response.text
    home = client.get("/", headers=ME).text
    assert "Streamflix" in home
    assert "$15.49" in home


def test_same_file_twice_adds_nothing(client):
    upload(client, monthly_csv(range(1, 7)))
    assert "already imported" in upload(client, monthly_csv(range(1, 7))).text


def test_overlapping_exports_skip_rows_already_imported(client):
    upload(client, monthly_csv(range(1, 5)))
    response = upload(client, monthly_csv(range(3, 8)))
    assert "3 new transactions, 2 already here" in response.text


def test_identical_rows_within_one_file_are_both_kept(client):
    content = b"Date,Description,Amount\n2026-01-05,CORNER CAFE,-4.00\n2026-01-05,CORNER CAFE,-4.00\n"
    assert "2 new transactions" in upload(client, content).text


def test_each_person_only_sees_their_own_data(client):
    upload(client, monthly_csv(range(1, 7)), headers=ME)
    assert "Streamflix" not in client.get("/", headers=PARTNER).text
    assert client.get("/merchant", params={"name": "Streamflix"}, headers=PARTNER).status_code == 404


def test_marking_cancel_moves_it_and_counts_savings(client):
    upload(client, monthly_csv(range(1, 7)))
    page = visible_text(client.post("/status", headers={**ME, **HTMX},
                                    data={"merchant": "Streamflix", "status": "cancel"}).text)
    assert "To cancel 1" in page
    assert "Marked to cancel $186 /yr saved" in page  # 15.49 x 12 = 185.88


def test_state_changes_require_htmx_header(client):
    upload(client, monthly_csv(range(1, 7)))
    assert client.post("/status", headers=ME, data={"merchant": "Streamflix", "status": "keep"}).status_code == 403
    assert client.post("/import", headers=ME, files={"file": ("a.csv", b"x")}, data={"account": "A"}).status_code == 403


def test_renaming_two_merchants_to_one_name_merges_them(client):
    upload(client, monthly_csv(range(1, 4), description="STREAMFLIX.COM", amount="-15.49"))
    upload(client, monthly_csv(range(4, 7), description="SFX*STREAMFLIX PREMIUM", amount="-15.49"), name="b.csv")
    assert client.get("/", headers=ME).text.count('class="sub-row"') == 2

    page = client.post("/merchants/rename", headers={**ME, **HTMX},
                       data={"key": "SFX STREAMFLIX PREMIUM", "name": "Streamflix"}).text
    assert page.count('name="name" value="Streamflix"') == 2
    assert client.get("/", headers=ME).text.count('class="sub-row"') == 1
    detail = client.get("/merchant", params={"name": "Streamflix"}, headers=ME).text
    assert "Monthly" in detail
    assert detail.count("$15.49") >= 6
