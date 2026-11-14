from dataclasses import dataclass
from datetime import date, timedelta
from statistics import median

# Average days per period, so yearly cost works out exactly.
CADENCES = {
    "weekly": 7.0,
    "biweekly": 14.0,
    "monthly": 365.25 / 12,
    "quarterly": 365.25 / 4,
    "yearly": 365.25,
}

MIN_CHARGES = 3
GAP_TOLERANCE = 0.20
MIN_REGULAR_GAP_SHARE = 0.75
AMOUNT_TOLERANCE = 0.25
MIN_STABLE_AMOUNT_SHARE = 0.75  # leaves room for a price hike without dropping the subscription
PRICE_CHANGE_THRESHOLD = 0.02
STOPPED_AFTER_PERIODS = 1.5


@dataclass(frozen=True)
class Charge:
    posted_on: date
    amount_cents: int  # positive = money spent


@dataclass
class Subscription:
    merchant: str
    cadence: str
    charges: list[Charge]  # oldest first

    @property
    def period_days(self) -> float:
        return CADENCES[self.cadence]

    @property
    def latest_cents(self) -> int:
        return self.charges[-1].amount_cents

    @property
    def last_charged(self) -> date:
        return self.charges[-1].posted_on

    @property
    def yearly_cents(self) -> int:
        return round(self.latest_cents * 365.25 / self.period_days)

    @property
    def monthly_cents(self) -> int:
        return round(self.yearly_cents / 12)

    @property
    def price_change(self) -> tuple[int, int] | None:
        """(old, current) price if a fixed price changed within the last year."""
        amounts = [charge.amount_cents for charge in self.charges]
        start = len(amounts) - 1
        while start > 0 and amounts[start - 1] == self.latest_cents:
            start -= 1
        if start < 2:
            return None
        old = amounts[start - 1]
        # Without two equal charges before the change it's a metered bill, not a price change.
        if amounts[start - 2] != old:
            return None
        if (self.last_charged - self.charges[start].posted_on).days > 365:
            return None
        if abs(self.latest_cents - old) > old * PRICE_CHANGE_THRESHOLD:
            return old, self.latest_cents
        return None

    @property
    def next_expected(self) -> date:
        return self.last_charged + timedelta(days=round(self.period_days))

    def is_stopped(self, today: date) -> bool:
        return (today - self.last_charged).days > self.period_days * STOPPED_AFTER_PERIODS


def detect(charges_by_merchant: dict[str, list[Charge]]) -> list[Subscription]:
    subscriptions = []
    for merchant, charges in charges_by_merchant.items():
        charges = sorted(charges, key=lambda charge: charge.posted_on)
        cadence = _cadence(charges)
        if cadence and _amounts_are_stable(charges):
            subscriptions.append(Subscription(merchant, cadence, charges))
    return sorted(subscriptions, key=lambda sub: -sub.yearly_cents)


def _cadence(charges: list[Charge]) -> str | None:
    if len(charges) < MIN_CHARGES:
        return None
    gaps = [(b.posted_on - a.posted_on).days for a, b in zip(charges, charges[1:])]
    typical_gap = median(gaps)
    for name, days in CADENCES.items():
        if abs(typical_gap - days) <= days * GAP_TOLERANCE:
            regular = sum(abs(gap - days) <= days * GAP_TOLERANCE for gap in gaps)
            return name if regular / len(gaps) >= MIN_REGULAR_GAP_SHARE else None
    return None


def _amounts_are_stable(charges: list[Charge]) -> bool:
    typical = median(charge.amount_cents for charge in charges)
    stable = sum(abs(charge.amount_cents - typical) <= typical * AMOUNT_TOLERANCE for charge in charges)
    return stable / len(charges) >= MIN_STABLE_AMOUNT_SHARE


def monthly_totals(subscriptions: list[Subscription], months: list[date]) -> list[int]:
    """Cents charged by these subscriptions in each month (months given as first-of-month dates)."""
    totals = {month: 0 for month in months}
    for sub in subscriptions:
        for charge in sub.charges:
            month = charge.posted_on.replace(day=1)
            if month in totals:
                totals[month] += charge.amount_cents
    return [totals[month] for month in months]
