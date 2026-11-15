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


def test_subscription_is_still_found_after_a_move_changed_the_bill():
    # Old plan, a prorated final bill, a first bill with an install fee, then the new plan.
    before = monthly(date(2025, 1, 10), [5000] * 6)
    move = [Charge(date(2025, 7, 3), 2600), Charge(date(2025, 7, 22), 14200)]
    after = monthly(date(2025, 8, 22), [9500] * 8)
    [sub] = detect({"CABLECO": before + move + after})
    assert sub.cadence == "monthly"
    assert sub.latest_cents == 9500
    assert len(sub.charges) == 16


def test_a_few_regular_charges_after_many_random_ones_are_not_a_subscription():
    days = [0, 3, 11, 12, 30, 33, 47, 60, 61, 75, 90, 98, 130, 160, 190]
    amounts = [4210, 1875, 6230, 980, 3311, 5120, 2290, 4410, 760, 3890, 5555, 2101, 3000, 3020, 2990]
    charges = [Charge(date(2026, 1, 1) + timedelta(days=d), a) for d, a in zip(days, amounts)]
    assert detect({"GROCER": charges}) == []


def test_plans_billed_under_one_name_are_found_separately():
    charges = monthly(date(2026, 1, 3), [999] * 6) + monthly(date(2026, 1, 19), [299] * 6)
    subs = detect({"APPLE": charges})
    assert {(sub.merchant, sub.name, sub.latest_cents) for sub in subs} == {
        ("APPLE", "APPLE · $9.99", 999), ("APPLE", "APPLE · $2.99", 299)}


def test_a_regular_bill_with_changing_amounts_is_a_variable_bill():
    [sub] = detect({"CARD AUTOPAY": monthly(date(2025, 1, 25), [41233, 18710, 99502, 26045, 53390, 12001, 70412])})
    assert sub.variable
    assert sub.cadence == "monthly"
    assert sub.price_change is None
    assert sub.name == "CARD AUTOPAY"


def test_weekly_shopping_with_changing_amounts_is_not_a_bill():
    charges = [Charge(date(2026, 1, 3) + timedelta(weeks=w), a)
               for w, a in enumerate([8211, 4520, 12090, 6310, 9930, 3870, 10450, 5120])]
    assert detect({"GROCER": charges}) == []


def test_random_purchases_in_a_narrow_price_band_are_not_a_plan():
    purchases = [("2025-09-07", 1357), ("2025-09-30", 2559), ("2025-10-09", 1201), ("2025-10-19", 2556),
                 ("2025-11-06", 1624), ("2025-11-20", 2505), ("2025-11-25", 2032), ("2026-01-17", 2618),
                 ("2026-01-22", 1320), ("2026-02-28", 1189), ("2026-03-03", 1719), ("2026-03-05", 1944),
                 ("2026-03-31", 1572), ("2026-05-07", 2333), ("2026-05-23", 1971), ("2026-05-30", 2044),
                 ("2026-06-02", 1132), ("2026-06-06", 2424), ("2026-06-08", 2070), ("2026-07-01", 2186),
                 ("2026-07-16", 1642), ("2026-10-03", 1654)]
    charges = [Charge(date.fromisoformat(day), cents) for day, cents in purchases]
    assert detect({"TACO PLACE": charges}) == []


def test_a_utility_bill_with_a_few_similar_months_is_still_a_variable_bill():
    amounts = [5512, 14230, 7870, 13011, 6640, 14390, 5980, 9020, 11800, 10120, 12088, 11540, 10930, 12500]
    [sub] = detect({"CITY POWER": monthly(date(2025, 9, 8), amounts)})
    assert sub.variable
