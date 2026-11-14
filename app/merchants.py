import re

# Card processors prefix the real merchant, e.g. "SQ *BLUE BOTTLE" or "PAYPAL *HULU".
_PROCESSOR_PREFIX = re.compile(r"\b(SQ|TST|SP|PP|PAYPAL|GOOGLE|APL|IN)\s*\*\s*")
_DATE = re.compile(r"\b\d{1,2}/\d{1,2}(/\d{2,4})?\b")
_DOMAIN_SUFFIX = re.compile(r"\.(COM|NET|ORG|IO|CO|TV)\b")
_NOISE_WORDS = {
    "POS", "ACH", "DEBIT", "CREDIT", "CARD", "PURCHASE", "RECURRING", "PAYMENT",
    "AUTHORIZED", "ON", "CHECKCARD", "WWW", "PPD", "CCD", "WEB", "BILL",
}


def merchant_key(description: str) -> str:
    """Collapse a raw bank description into a stable grouping key, e.g. 'NETFLIX'."""
    text = description.upper().strip()
    text = _DATE.sub(" ", text)
    text = _PROCESSOR_PREFIX.sub("", text)
    text = _DOMAIN_SUFFIX.sub(" ", text)
    tokens = [
        token
        for token in re.split(r"[^A-Z0-9&']+", text)
        if token and not any(char.isdigit() for char in token) and token not in _NOISE_WORDS
    ]
    # Trailing two-letter tokens are almost always a state code.
    while len(tokens) > 1 and len(tokens[-1]) == 2:
        tokens.pop()
    return " ".join(tokens[:3]) or description.strip().upper()


def display_name(key: str) -> str:
    return " ".join(word.capitalize() for word in key.split())
