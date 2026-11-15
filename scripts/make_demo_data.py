"""Write synthetic exports to data/demo/ for trying the app. Nothing here is real.

  demo-card.csv          you: a card, 14 months of history
  demo-card-recent.csv   you: a later download of the same card, overlapping the last 100 days
  demo-checking.csv      you: checking, in a debit/credit column format
  demo-partner.ofx       a second person: their own card, as OFX
"""

import csv
import random
from datetime import date, timedelta
from pathlib import Path

OUT = Path("data/demo")
random.seed(7)
today = date.today()
start = today.replace(day=1) - timedelta(days=420)


def months(day: int, until: date = today, every: int = 1):
    current = date(start.year, start.month, 1)
    count = 0
    while True:
        charge = current.replace(day=day)
        if charge > until:
            return
        if charge >= start and count % every == 0:
            yield charge
        count += 1
        current = (current + timedelta(days=32)).replace(day=1)


def write_csv(name: str, header: list[str], rows: list[tuple]) -> None:
    with (OUT / name).open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)
    print(f"Wrote {len(rows)} synthetic rows to {OUT / name}")


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

OUT.mkdir(parents=True, exist_ok=True)
card = [(when.strftime("%m/%d/%Y"), description, amount) for when, description, amount in sorted(rows)]
write_csv("demo-card.csv", ["Date", "Description", "Amount"], card)

# The same card downloaded again later: every row is already in demo-card.csv, so importing it adds nothing.
recent = [row for row, (when, _, _) in zip(card, sorted(rows)) if when >= today - timedelta(days=100)]
write_csv("demo-card-recent.csv", ["Date", "Description", "Amount"], recent)

# Checking uses separate Debit/Credit columns, like many bank exports.
rng = random.Random(11)
checking = []
for when in months(1):
    checking.append((when, "ACH DEBIT MAPLE COURT APTS RENT", "1850.00", ""))  # regular, so it shows up: mark it "Not a subscription"
for when in months(8):
    checking.append((when, "CITY POWER AND LIGHT", f"{rng.uniform(55, 150):.2f}", ""))  # swings in amount, so it shows up as a variable bill
for when in months(12):
    checking.append((when, "FIBERNET INTERNET", "70.00", ""))
for when in months(5, every=3):
    checking.append((when, "HOMESAFE INSURANCE QTRLY", "180.00", ""))
payday = start + timedelta(days=4)
while payday <= today:
    checking.append((payday, "PAYROLL EXAMPLE CO", "", "2100.00"))  # money in is ignored
    payday += timedelta(days=14)
for _ in range(25):
    when = start + timedelta(days=rng.randrange((today - start).days))
    checking.append((when, rng.choice(["ATM WITHDRAWAL 2231", "CHECK 1043", "VENMO PAYMENT"]), f"{rng.uniform(20, 200):.2f}", ""))
write_csv("demo-checking.csv", ["Posting Date", "Description", "Debit", "Credit"],
          [(when.isoformat(), description, debit, credit) for when, description, debit, credit in sorted(checking)])

# A second person's card, as OFX. Streamflix appears for both people: each sees only their own.
rng = random.Random(23)
partner = []
for when in months(16):
    partner.append((when, "STREAMFLIX.COM", "-15.49"))
for when in months(6):
    partner.append((when, "TUNEWAVE MUSIC", "-10.99"))
for when in months(19):
    partner.append((when, "NEWSDAILY DIGITAL", "-6.99" if when > today - timedelta(days=60) else "-4.99"))
meal_day = today - timedelta(days=200)
while meal_day <= today - timedelta(days=50):  # weekly, then stopped about 7 weeks ago
    partner.append((meal_day, "FRESHBOX MEALS", "-64.99"))
    meal_day += timedelta(days=7)
for _ in range(90):
    when = start + timedelta(days=rng.randrange((today - start).days))
    place, low, high = rng.choice([("SUNRISE BAKERY", 5, 18), ("GREENLEAF GROCERY 0441", 25, 120), ("PET PANTRY", 15, 70)])
    partner.append((when, place, f"-{rng.uniform(low, high):.2f}"))

transactions = "\n".join(
    f"<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>{when:%Y%m%d}<TRNAMT>{amount}<FITID>{i}<NAME>{name}</STMTTRN>"
    for i, (when, name, amount) in enumerate(sorted(partner), start=1)
)
ofx = f"""OFXHEADER:100
DATA:OFXSGML
VERSION:102
SECURITY:NONE
ENCODING:USASCII
CHARSET:1252
COMPRESSION:NONE
OLDFILEUID:NONE
NEWFILEUID:NONE

<OFX><SIGNONMSGSRSV1><SONRS><STATUS><CODE>0<SEVERITY>INFO</STATUS><DTSERVER>{today:%Y%m%d}<LANGUAGE>ENG</SONRS></SIGNONMSGSRSV1>
<BANKMSGSRSV1><STMTTRNRS><TRNUID>1<STATUS><CODE>0<SEVERITY>INFO</STATUS>
<STMTRS><CURDEF>USD<BANKACCTFROM><BANKID>000000000<ACCTID>0000<ACCTTYPE>CHECKING</BANKACCTFROM>
<BANKTRANLIST><DTSTART>{start:%Y%m%d}<DTEND>{today:%Y%m%d}
{transactions}
</BANKTRANLIST><LEDGERBAL><BALAMT>0.00<DTASOF>{today:%Y%m%d}</LEDGERBAL></STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>
"""
(OUT / "demo-partner.ofx").write_text(ofx)
print(f"Wrote {len(partner)} synthetic rows to {OUT / 'demo-partner.ofx'}")
