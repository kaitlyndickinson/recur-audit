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
# Judge the latest regular stretch, so a move or plan change doesn't hide a subscription, but only when
# that stretch is a good share of the charges, so a lucky few among random purchases don't count.
MIN_RUN_SHARE = 1 / 3
# App stores and PayPal bill each plan separately under one name; charges this close in price are one plan.
SAME_PLAN_TOLERANCE = 0.05
# Bills like a card autopay change every time, so only their timing can mark them as recurring.
VARIABLE_BILL_MIN_CHARGES = 6


@dataclass(frozen=True)
class Charge:
    posted_on: date
    amount_cents: int  # positive = money spent
    account: str = ""


@dataclass
class Subscription:
    merchant: str
    cadence: str
    charges: list[Charge]  # oldest first
    variable: bool = False
    plan_cents: int | None = None  # set when this is one of several plans billed under the merchant's name

    @property
    def name(self) -> str:
        """What it's listed and marked under; the merchant alone unless it's one of several plans."""
        if self.plan_cents is None:
            return self.merchant
        return f"{self.merchant} · ${self.plan_cents // 100:,}.{self.plan_cents % 100:02d}"

    @property
    def accounts(self) -> list[str]:
        return sorted({charge.account for charge in self.charges if charge.account})

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
        typical = median(charge.amount_cents for charge in self.charges[-6:]) if self.variable else self.latest_cents
        return round(typical * 365.25 / self.period_days)

    @property
    def monthly_cents(self) -> int:
        return round(self.yearly_cents / 12)

    @property
    def price_change(self) -> tuple[int, int] | None:
        """(old, current) price if a fixed price changed within the last year."""
        if self.variable:
            return None
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
        found = _subscription(merchant, charges)
        if found:
            subscriptions.append(found)
            continue
        plans = _plans(charges)
        if len(plans) > 1:
            for plan in plans:
                found = _subscription(merchant, plan, plan_cents=round(median(c.amount_cents for c in plan)))
                if found:
                    subscriptions.append(found)
    return sorted(subscriptions, key=lambda sub: -sub.yearly_cents)


def _subscription(merchant: str, charges: list[Charge], plan_cents: int | None = None) -> Subscription | None:
    """Look for the longest recent run of charges that recurs, preferring a fixed price to a variable bill."""
    runs = [charges[start:] for start in range(len(charges) - MIN_CHARGES + 1)
            if len(charges) - start >= len(charges) * MIN_RUN_SHARE]
    for run in runs:
        cadence = _cadence(run)
        if cadence and _amounts_are_stable(run):
            return Subscription(merchant, cadence, charges, plan_cents=plan_cents)
    for run in runs:
        cadence = _cadence(run)
        if cadence and CADENCES[cadence] >= CADENCES["monthly"] and len(run) >= VARIABLE_BILL_MIN_CHARGES:
            return Subscription(merchant, cadence, charges, variable=True, plan_cents=plan_cents)
    return None


def _plans(charges: list[Charge]) -> list[list[Charge]]:
    """Charges grouped by price, oldest first in each group; groups too small to recur are dropped."""
    groups: list[list[Charge]] = []
    for charge in sorted(charges, key=lambda charge: charge.amount_cents):
        if groups and charge.amount_cents <= groups[-1][0].amount_cents * (1 + SAME_PLAN_TOLERANCE):
            groups[-1].append(charge)
        else:
            groups.append([charge])
    return [sorted(group, key=lambda charge: charge.posted_on) for group in groups if len(group) >= MIN_CHARGES]


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
