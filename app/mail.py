"""Group an old mailbox by sender and propose what to do with each. Provider-neutral: no Gmail here."""

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from email.utils import parseaddr

ACTIONS = {
    "move": "Move to new address",  # shown as "Move to <address>" once the person sets one
    "unsubscribe": "Unsubscribe & trash",
    "spam": "Spam",
    "keep": "Keep",
}

# Mail you'd regret missing: it follows you to the new address.
IMPORTANT = re.compile(r"statement|password|security|sign.?in|verif|2-step|two-step|payment|bill\b|invoice|tax|"
                       r"appointment|prescription|policy|account (?:alert|notice|update)", re.IGNORECASE)
# An account somewhere (orders, bookings): kept, since these often carry an unsubscribe header too.
ACCOUNT = re.compile(r"\border\b|receipt|shipped|deliver|your account|welcome|confirm|booking|reservation", re.IGNORECASE)
# Subjects that mean this sender charges you something: worth cancelling or moving, not just unsubscribing.
# Phrases bills use and ads don't. Prices ("$5 off") and bare words like "subscription" are left out on
# purpose: marketing is full of them.
BILLING = re.compile(
    r"receipt|invoice|billing statement|bill is (?:ready|available)|your (?:monthly )?bill"
    r"|payment (?:was |has been )?(?:received|processed|scheduled|confirmed|declined|failed|due|reminder)"
    r"|auto ?-?pay|(?:subscription|membership|plan) (?:has been |was |is |will )?renew|renewal (?:notice|reminder|confirmation)"
    r"|trial (?:ends|ending|expires|is ending)|you(?:'ve| have)? been charged|thanks for your payment",
    re.IGNORECASE)
MARKETING = {"PROMOTIONS", "SOCIAL", "FORUMS"}
MIN_BILLS = 2  # one receipt is a purchase; billing emails that repeat are something charging you
BULK_WITHOUT_LINK = 3  # this many promos with no unsubscribe link reads as junk, not a one-off


@dataclass
class Sender:
    address: str
    name: str
    messages: int
    last_seen: date
    categories: Counter = field(default_factory=Counter)  # mailbox tab -> count, e.g. PROMOTIONS
    subjects: list[str] = field(default_factory=list)  # newest first; only used to classify, only the first is kept
    unsubscribe_url: str | None = None  # https only
    one_click: bool = False  # the sender supports RFC 8058 one-click unsubscribe
    has_list_unsubscribe: bool = False  # any unsubscribe header, including mailto

    @property
    def domain(self) -> str:
        return self.address.rpartition("@")[2]

    @property
    def costs_money(self) -> bool:
        """Charges you on repeat (a monthly AWS bill, a streaming plan), judged from its emails alone."""
        return looks_like_billing(self.subjects, self.category)

    @property
    def category(self) -> str:
        return self.categories.most_common(1)[0][0] if self.categories else "PERSONAL"


def propose(sender: Sender, paid_merchants: set[str]) -> str:
    if _pays(sender, paid_merchants) or sender.costs_money:
        return "move"  # something you pay for: its mail belongs at the new address
    subjects = " | ".join(sender.subjects)
    if sender.category in ("PROMOTIONS", "SOCIAL", "FORUMS"):
        if sender.has_list_unsubscribe:
            return "unsubscribe"
        return "spam" if sender.messages >= BULK_WITHOUT_LINK else "keep"
    if IMPORTANT.search(subjects):
        return "move"
    if ACCOUNT.search(subjects):
        return "keep"
    return "unsubscribe" if sender.has_list_unsubscribe else "keep"


def _pays(sender: Sender, paid_merchants) -> bool:
    return paid_merchant(sender.name, sender.address, paid_merchants) is not None


def paid_merchant(name: str, address: str, merchants) -> str | None:
    """The bank-data subscription this sender looks like, e.g. Streamflix for Streamflix <hello@mail.streamflix.com>."""
    haystack = _squash(name) + " " + address.rpartition("@")[2].lower()
    return next((m for m in merchants if len(_squash(m)) >= 4 and _squash(m) in haystack), None)


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


@dataclass(frozen=True)
class Message:
    """One email's headers, as any mail source provides them."""
    sender: str  # the raw From header
    subject: str
    received: date
    category: str  # mailbox tab, e.g. PROMOTIONS; PERSONAL when the source has none
    list_unsubscribe: str = ""  # raw List-Unsubscribe header
    list_unsubscribe_post: str = ""  # raw List-Unsubscribe-Post header


def group(messages: list[Message]) -> list[Sender]:
    senders: dict[str, Sender] = {}
    for message in sorted(messages, key=lambda m: m.received, reverse=True):
        name, address = parseaddr(message.sender)
        address = address.lower()
        if not address:
            continue
        sender = senders.get(address)
        if sender is None:
            # Newest message first, so the name and unsubscribe link are the current ones.
            urls = re.findall(r"<(https://[^>]+)>", message.list_unsubscribe)
            sender = senders[address] = Sender(
                address=address, name=name or address, messages=0, last_seen=message.received,
                unsubscribe_url=urls[0] if urls else None,
                one_click=bool(urls) and "one-click" in message.list_unsubscribe_post.lower(),
            )
        sender.messages += 1
        sender.categories[message.category] += 1
        sender.has_list_unsubscribe |= bool(message.list_unsubscribe)
        if len(sender.subjects) < 20:
            sender.subjects.append(message.subject)
    return list(senders.values())


def looks_like_billing(subjects: list[str], category: str, needed: int = MIN_BILLS) -> bool:
    """Gmail files bills under Updates or Primary, never Promotions, so marketing never counts."""
    return category not in MARKETING and sum(bool(BILLING.search(s)) for s in subjects) >= needed
