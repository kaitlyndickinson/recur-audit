"""Write a synthetic card export to data/demo/demo-card.csv for trying the app. Nothing here is real."""

import csv
import random
from datetime import date, timedelta
from pathlib import Path

random.seed(7)
today = date.today()
start = today.replace(day=1) - timedelta(days=420)


def months(day: int, until: date = today):
    current = date(start.year, start.month, 1)
    while True:
        charge = current.replace(day=day)
        if charge > until:
            return
        if charge >= start:
            yield charge
        current = (current + timedelta(days=32)).replace(day=1)


rows = []
for when in months(14):
    rows.append((when, "STREAMFLIX.COM 866-555-0100 CA", "22.99" if when > today - timedelta(days=75) else "17.99"))
for when in months(3):
    rows.append((when, "PAYPAL *MUSICO 402-555-0123", "11.99"))
for when in months(21):
    rows.append((when, "ACH DEBIT IRONWORKS GYM PPD ID 4471", "49.00"))
for when in months(9, until=today - timedelta(days=110)):
    rows.append((when, "SQ *SUNDAY PAPER DIGITAL", "8.00"))
for when in months(27):
    rows.append((when, "CLOUDBOX STORAGE 1TB", "2.99"))
for when in months(17):
    rows.append((when, "WIRELESS EXAMPLE AUTOPAY", f"{random.uniform(84, 92):.2f}"))
rows.append((today - timedelta(days=300), "APPDEV PRO ANNUAL", "99.00"))
rows.append((today - timedelta(days=665), "APPDEV PRO ANNUAL", "99.00"))
rows.append((today - timedelta(days=1030), "APPDEV PRO ANNUAL", "99.00"))
for _ in range(160):
    when = start + timedelta(days=random.randrange((today - start).days))
    place, low, high = random.choice([
        ("CORNER CAFE #12", 4, 9), ("GREENLEAF GROCERY 0441", 30, 140), ("FUEL STOP 8812", 35, 60),
        ("BOOKNOOK STORE", 12, 40), ("TACO PLACE", 11, 28), ("HARDWARE BARN #301", 8, 95),
    ])
    rows.append((when, place, f"{random.uniform(low, high):.2f}"))
for when in months(25):
    rows.append((when, "ONLINE PAYMENT - THANK YOU", "-1200.00"))

out = Path("data/demo/demo-card.csv")
out.parent.mkdir(parents=True, exist_ok=True)
with out.open("w", newline="") as f:
    writer = csv.writer(f)
    writer.writerow(["Date", "Description", "Amount"])
    for when, description, amount in sorted(rows):
        writer.writerow([when.strftime("%m/%d/%Y"), description, amount])
print(f"Wrote {len(rows)} synthetic rows to {out}")
