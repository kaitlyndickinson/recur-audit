"""Merchant categories, with suggestions from a language model you host yourself (any OpenAI-compatible server)."""

import json
import os

import httpx

CATEGORIES = [
    "Groceries", "Food & drink", "Bills & utilities", "Subscriptions & streaming", "Insurance", "Loans",
    "Housing", "Transportation", "Shopping", "Health", "Transfers & payments", "Income", "Other",
]
BATCH = 40  # merchants per request; small enough that a local model answers each in seconds
PROMPT = (
    "Categorize each merchant from a personal bank statement. Each line is a merchant name and one bank "
    "description. Answer with one category per line, in order. Paying a credit card bill and sending money "
    "to a person (Zelle, Venmo) are Transfers & payments. Categories: " + ", ".join(CATEGORIES)
)
transport = None  # tests swap in a fake model


class ModelUnavailable(Exception):
    pass


def configured() -> bool:
    return bool(os.environ.get("RECUR_AUDIT_LLM_URL"))


def suggest(merchants: list[tuple[str, str]]) -> dict[str, str]:
    """Suggest a category for each (name, example description). Nothing else about the merchant is sent."""
    suggestions = {}
    try:
        with httpx.Client(timeout=300, transport=transport) as client:
            for start in range(0, len(merchants), BATCH):
                batch = merchants[start:start + BATCH]
                answers = _ask(client, batch)
                # The model's output is untrusted: keep only answers that are on the list.
                suggestions.update({name: answer for (name, _), answer in zip(batch, answers) if answer in CATEGORIES})
    except (httpx.HTTPError, ValueError, KeyError) as error:
        raise ModelUnavailable(str(error) or type(error).__name__) from error
    return suggestions


def _ask(client: httpx.Client, batch: list[tuple[str, str]]) -> list:
    lines = "\n".join(f"{number}. {name} | {example}" for number, (name, example) in enumerate(batch, start=1))
    schema = {"type": "array", "items": {"type": "string", "enum": CATEGORIES},
              "minItems": len(batch), "maxItems": len(batch)}
    response = client.post(os.environ["RECUR_AUDIT_LLM_URL"].rstrip("/") + "/chat/completions", json={
        "model": os.environ.get("RECUR_AUDIT_LLM_MODEL", ""),
        "temperature": 0,
        "chat_template_kwargs": {"enable_thinking": False},  # a one-word answer doesn't need reasoning
        "response_format": {"type": "json_schema", "json_schema": {"name": "categories", "schema": schema}},
        "messages": [{"role": "system", "content": PROMPT}, {"role": "user", "content": lines}],
    })
    response.raise_for_status()
    answers = json.loads(response.json()["choices"][0]["message"]["content"])
    if not isinstance(answers, list):
        raise ValueError("the model didn't answer with a list")
    return answers
