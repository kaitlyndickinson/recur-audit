import html
import json
from datetime import date, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from app import categories
from app.main import app
from tests.test_routes import HTMX, ME, PARTNER, monthly_csv, months_back, upload, visible_text


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("RECUR_AUDIT_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("RECUR_AUDIT_LOCAL_USER", raising=False)
    monkeypatch.delenv("RECUR_AUDIT_LLM_URL", raising=False)
    return TestClient(app, base_url="http://localhost")


@pytest.fixture
def llm(monkeypatch):
    """A fake model that answers from `answers` by merchant name and records what it was sent."""
    sent, answers = [], {}

    def fake(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sent.append(body)
        lines = body["messages"][-1]["content"].splitlines()
        names = [line.split(". ", 1)[1].split(" | ")[0] for line in lines]
        content = json.dumps([answers.get(name, "Other") for name in names])
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    monkeypatch.setenv("RECUR_AUDIT_LLM_URL", "http://llm.test/v1")
    monkeypatch.setattr(categories, "transport", httpx.MockTransport(fake))
    return sent, answers


def set_category(client, merchant, category, headers=ME):
    return client.post("/merchants/category", headers={**headers, **HTMX},
                       data={"merchant": merchant, "category": category})


def test_a_category_can_be_set_and_filtered_on(client):
    upload(client, monthly_csv(range(1, 4)))
    upload(client, monthly_csv(range(1, 4), description="FRESHMART", amount="-82.10"), name="b.csv")
    set_category(client, "Freshmart", "Groceries")

    page = client.get("/merchants", params={"category": "Groceries"}, headers=ME).text
    assert "Freshmart" in page and "Streamflix" not in page
    uncategorized = client.get("/merchants", params={"category": "none"}, headers=ME).text
    assert "Streamflix" in uncategorized and 'value="Freshmart"' not in uncategorized
    shopping = visible_text(client.get("/merchants", params={"category": "Shopping"}, headers=ME).text)
    assert "No merchants in this category." in shopping


def test_unknown_categories_are_refused(client):
    upload(client, monthly_csv(range(1, 4)))
    assert set_category(client, "Streamflix", "Yachts").status_code == 400


def test_categories_belong_to_each_person(client):
    upload(client, monthly_csv(range(1, 4)), headers=ME)
    upload(client, monthly_csv(range(1, 4)), headers=PARTNER)
    set_category(client, "Streamflix", "Subscriptions & streaming", headers=ME)
    partner = client.get("/merchants", params={"category": "Subscriptions & streaming"}, headers=PARTNER).text
    assert "Streamflix" not in partner


def test_renaming_keeps_the_category(client):
    upload(client, monthly_csv(range(1, 4)))
    set_category(client, "Streamflix", "Subscriptions & streaming")
    client.post("/merchants/rename", headers={**ME, **HTMX}, data={"key": "STREAMFLIX", "name": "Streamflix TV"})
    page = client.get("/merchants", params={"category": "Subscriptions & streaming"}, headers=ME).text
    assert "Streamflix TV" in page


def test_suggest_is_hidden_without_a_model(client):
    upload(client, monthly_csv(range(1, 4)))
    assert "Suggest categories" not in client.get("/merchants", headers=ME).text


def test_suggest_fills_only_uncategorized_merchants_and_sends_no_amounts_or_dates(client, llm):
    sent, answers = llm
    answers.update({"Streamflix": "Subscriptions & streaming", "Freshmart": "Groceries"})
    upload(client, monthly_csv(range(1, 4)))
    upload(client, monthly_csv(range(1, 4), description="FRESHMART", amount="-82.10"), name="b.csv")
    set_category(client, "Freshmart", "Shopping")

    page = visible_text(client.post("/merchants/suggest", headers={**ME, **HTMX}).text)
    assert "Suggested a category for 1 merchant." in page
    groceries = client.get("/merchants", params={"category": "Groceries"}, headers=ME).text
    streaming = client.get("/merchants", params={"category": "Subscriptions & streaming"}, headers=ME).text
    assert "Freshmart" not in groceries  # the person's own choice stays
    assert "Streamflix" in streaming
    asked = json.dumps(sent)
    assert "STREAMFLIX.COM" in asked and "Freshmart" not in asked
    assert "15.49" not in asked and str(months_back(11)[0]) not in asked


def test_answers_outside_the_list_are_ignored(client, llm):
    sent, answers = llm
    answers["Streamflix"] = "Yachts"
    upload(client, monthly_csv(range(1, 4)))
    client.post("/merchants/suggest", headers={**ME, **HTMX})
    assert "Streamflix" in client.get("/merchants", params={"category": "none"}, headers=ME).text


def test_an_unreachable_model_shows_an_error(client, monkeypatch):
    def down(request):
        raise httpx.ConnectError("refused")

    monkeypatch.setenv("RECUR_AUDIT_LLM_URL", "http://llm.test/v1")
    monkeypatch.setattr(categories, "transport", httpx.MockTransport(down))
    upload(client, monthly_csv(range(1, 4)))
    page = visible_text(client.post("/merchants/suggest", headers={**ME, **HTMX}).text)
    assert "Couldn’t reach the model" in page


def charges_at(description, cents, days):
    lines = ["Date,Description,Amount"] + [f"{day},{description},-{cents / 100:.2f}" for day in days]
    return ("\n".join(lines) + "\n").encode()


def test_each_category_shows_what_was_spent_in_the_period(client):
    recent, old = months_back(3), [date.today() - timedelta(days=500)]
    upload(client, charges_at("FRESHMART", 5000, recent + old))
    upload(client, charges_at("CORNER CAFE", 450, recent), name="b.csv")
    set_category(client, "Freshmart", "Groceries")
    set_category(client, "Corner Cafe", "Food & drink")

    page = html.unescape(visible_text(client.get("/merchants", headers=ME).text))
    assert "Groceries $150" in page and "Food & drink $13.50" in page and "All $164" in page
    everything = visible_text(client.get("/merchants", params={"period": "all"}, headers=ME).text)
    assert "Groceries $200" in everything

    groceries = visible_text(client.get("/merchants", params={"category": "Groceries"}, headers=ME).text)
    assert "$150 on Groceries across 1 merchant in the last 12 months" in groceries


def test_changing_a_category_keeps_the_period_and_filter(client):
    upload(client, charges_at("FRESHMART", 5000, months_back(3)))
    page = client.post("/merchants/category", headers={**ME, **HTMX},
                       data={"merchant": "Freshmart", "category": "Groceries", "view_category": "none", "period": "3m"}).text
    assert 'aria-current="true">3M' in page.replace("\n", "").replace("  ", "")
    assert "No merchants in this category." in visible_text(page)
