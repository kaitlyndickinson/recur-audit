import re
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app.main import app

ME = {"Tailscale-User-Login": "alex@example.com"}
PARTNER = {"Tailscale-User-Login": "sam@example.com"}
HTMX = {"HX-Request": "true"}


def monthly_csv(months: range, description="STREAMFLIX.COM 866-555-0100 CA", amount="-15.49") -> bytes:
    """Month 1 is 11 months ago and month 11 is last month, so every charge falls in the default 12-month view."""
    days = months_back(11)
    lines = ["Date,Description,Amount"] + [f"{days[m - 1]},{description},{amount}" for m in months]
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


def test_merchant_page_highlights_the_subscriptions_tab(client):
    upload(client, monthly_csv(range(1, 7)))
    page = client.get("/merchant", params={"name": "Streamflix"}, headers=ME).text
    assert re.search(r'<a href="/" aria-current="page">', page)


def months_back(count: int) -> list[date]:
    """The 14th of each of the last `count` full months, oldest first, so tests don't depend on today's date."""
    first = date.today().replace(day=1)
    days = []
    for _ in range(count):
        first = (first - timedelta(days=1)).replace(day=1)
        days.append(first.replace(day=14))
    return days[::-1]


def charges_csv(days: list[date], amount="-15.49") -> bytes:
    lines = ["Date,Description,Amount"] + [f"{d},STREAMFLIX.COM 866-555-0100 CA,{amount}" for d in days]
    return ("\n".join(lines) + "\n").encode()


def test_period_limits_the_spend_total(client):
    upload(client, charges_csv(months_back(10)))
    assert "$30.98" in visible_text(client.get("/", params={"period": "3m"}, headers=ME).text)  # 2 full months + this one
    assert "$154.90" in visible_text(client.get("/", headers=ME).text)  # default 12 months covers all 10
    assert "$154.90" in visible_text(client.get("/", params={"period": "all"}, headers=ME).text)


def test_unknown_period_falls_back_to_12_months(client):
    upload(client, charges_csv(months_back(10)))
    page = client.get("/", params={"period": "bogus"}, headers=ME)
    assert page.status_code == 200
    assert re.search(r'period=12m"[^>]*aria-current="true"', page.text)


def test_changing_a_status_keeps_the_period(client):
    upload(client, charges_csv(months_back(10)))
    page = client.post("/status", headers={**ME, **HTMX},
                       data={"merchant": "Streamflix", "status": "keep", "period": "3m"}).text
    assert "$30.98" in visible_text(page)


def test_static_files_are_revalidated_so_style_changes_show_up(client):
    assert client.get("/static/app.css").headers["cache-control"] == "no-cache"


def test_list_only_shows_subscriptions_charged_in_the_period(client):
    upload(client, charges_csv(months_back(10)))
    yearly = months_back(33)
    upload(client, charges_csv([yearly[-9], yearly[-21], yearly[-33]], amount="-99.00").replace(b"STREAMFLIX.COM 866-555-0100 CA", b"APPDEV PRO ANNUAL"), name="b.csv")
    assert "Appdev Pro Annual" in client.get("/", headers=ME).text
    three_months = client.get("/", params={"period": "3m"}, headers=ME).text
    assert "Streamflix" in three_months
    assert "Appdev Pro Annual" not in three_months


def imports_page(client, headers=ME) -> str:
    return client.get("/import", headers=headers).text


def import_id(client, filename="export.csv") -> str:
    return re.search(rf'data-import-id="(\d+)"[^>]*>\s*<[^>]*>\s*{re.escape(filename)}', imports_page(client)).group(1)


def delete_import(client, id_, headers=ME):
    return client.post("/import/delete", headers={**headers, **HTMX}, data={"import_id": id_})


def test_imports_are_listed_for_their_owner_only(client):
    upload(client, monthly_csv(range(1, 7)), account="Checking")
    assert "export.csv" in imports_page(client)
    assert "Checking" in imports_page(client)
    assert "export.csv" not in imports_page(client, PARTNER)


def test_deleting_an_import_removes_its_transactions_and_stored_file(client, tmp_path):
    upload(client, monthly_csv(range(1, 7)))
    assert list((tmp_path / "raw").iterdir())
    delete_import(client, import_id(client))
    assert "export.csv" not in imports_page(client)
    assert "Streamflix" not in client.get("/", headers=ME).text
    assert not list((tmp_path / "raw").iterdir())


def test_deleting_an_import_keeps_rows_another_import_also_had(client):
    upload(client, monthly_csv(range(1, 7)), name="jan-jun.csv")
    upload(client, monthly_csv(range(4, 10)), name="apr-sep.csv")  # months 4-6 were skipped as duplicates
    delete_import(client, import_id(client, "jan-jun.csv"))
    assert "$92.94" in visible_text(client.get("/", headers=ME).text)  # months 4-9: 6 x 15.49


def test_people_cant_delete_each_others_imports(client):
    upload(client, monthly_csv(range(1, 7)))
    id_ = import_id(client)
    assert delete_import(client, id_, PARTNER).status_code == 404
    assert client.post("/import/delete", headers=ME, data={"import_id": id_}).status_code == 403
    assert "export.csv" in imports_page(client)


def test_deleting_keeps_the_stored_file_when_someone_else_imported_it_too(client, tmp_path):
    upload(client, monthly_csv(range(1, 7)))
    upload(client, monthly_csv(range(1, 7)), headers=PARTNER)
    delete_import(client, import_id(client))
    assert list((tmp_path / "raw").iterdir())
    assert "Streamflix" in client.get("/", headers=PARTNER).text
