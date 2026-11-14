from datetime import date, timedelta

from app.recurring import Charge, detect, monthly_totals


def monthly(start: date, amounts: list[int]) -> list[Charge]:
    charges = []
    for i, amount in enumerate(amounts):
        month = start.month - 1 + i
        charges.append(Charge(date(start.year + month // 12, month % 12 + 1, start.day), amount))
    return charges


def test_monthly_charges_are_detected_with_yearly_cost():
    [sub] = detect({"STREAMFLIX": monthly(date(2026, 1, 15), [1549] * 6)})
    assert sub.cadence == "monthly"
    assert sub.latest_cents == 1549
    assert sub.yearly_cents == 1549 * 12
    assert sub.price_change is None


def test_yearly_charges_are_detected():
    charges = [Charge(date(2023, 3, 2), 9900), Charge(date(2024, 3, 1), 9900), Charge(date(2025, 3, 3), 9900)]
    [sub] = detect({"CLOUDBOX": charges})
    assert sub.cadence == "yearly"
    assert sub.yearly_cents == 9900


def test_charges_can_arrive_in_any_order():
    charges = monthly(date(2026, 1, 5), [999] * 4)
    [sub] = detect({"MUSICO": list(reversed(charges))})
    assert sub.last_charged == date(2026, 4, 5)


def test_irregular_spacing_is_not_a_subscription():
    days = [0, 3, 40, 47, 120]
    charges = [Charge(date(2026, 1, 1) + timedelta(days=d), 2500) for d in days]
    assert detect({"CORNER CAFE": charges}) == []


def test_wildly_varying_amounts_are_not_a_subscription():
    charges = monthly(date(2026, 1, 10), [4200, 11850, 3100, 9020])
    assert detect({"GROCER": charges}) == []


def test_two_charges_are_not_enough():
    assert detect({"TRIALCO": monthly(date(2026, 1, 1), [500, 500])}) == []


def test_price_increase_is_flagged():
    [sub] = detect({"STREAMFLIX": monthly(date(2026, 1, 15), [1549, 1549, 1549, 1799])})
    assert sub.price_change == (1549, 1799)


def test_stopped_subscription_is_flagged():
    [sub] = detect({"GYMCO": monthly(date(2026, 1, 1), [3000] * 4)})
    assert not sub.is_stopped(today=date(2026, 5, 10))
    assert sub.is_stopped(today=date(2026, 6, 20))


def test_monthly_totals_sums_charges_per_month():
    subs = detect({
        "A": monthly(date(2026, 1, 3), [1000] * 3),
        "B": monthly(date(2026, 2, 20), [250] * 3),
    })
    months = [date(2026, m, 1) for m in (1, 2, 3, 4, 5)]
    assert monthly_totals(subs, months) == [1000, 1250, 1250, 250, 0]


def test_subscription_with_a_big_recent_price_hike_is_still_detected():
    [sub] = detect({"STREAMFLIX": monthly(date(2026, 1, 14), [1799] * 8 + [2299] * 2)})
    assert sub.price_change == (1799, 2299)


def test_variable_bill_is_not_reported_as_a_price_change():
    [sub] = detect({"PHONECO": monthly(date(2026, 1, 17), [8740, 8812, 8655, 8903, 8740, 9061])})
    assert sub.price_change is None


def test_price_change_from_over_a_year_ago_is_not_flagged():
    [sub] = detect({"GYMCO": monthly(date(2024, 1, 1), [4000] * 3 + [4900] * 16)})
    assert sub.price_change is None
