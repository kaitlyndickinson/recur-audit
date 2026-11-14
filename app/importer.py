import csv
import io
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

import ofxparse

DATE_COLUMNS = ["transaction date", "trans. date", "posted date", "posting date", "date"]
DESCRIPTION_COLUMNS = ["description", "payee", "merchant", "name", "memo"]
AMOUNT_COLUMNS = ["amount", "transaction amount"]
DEBIT_COLUMNS = ["debit", "withdrawal", "withdrawals"]
CREDIT_COLUMNS = ["credit", "deposit", "deposits"]
DATE_FORMATS = ["%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%b %d, %Y"]


class UnreadableExport(ValueError):
    """The file couldn't be read as a bank export; the message is shown to the user."""


@dataclass(frozen=True)
class Row:
    posted_on: date
    amount_cents: int  # negative = money out
    description: str


def parse(filename: str, content: bytes) -> list[Row]:
    if filename.lower().endswith((".ofx", ".qfx")):
        return _parse_ofx(content)
    return _parse_csv(content)


def _parse_ofx(content: bytes) -> list[Row]:
    try:
        ofx = ofxparse.OfxParser.parse(io.BytesIO(content))
    except Exception as error:
        raise UnreadableExport(f"Couldn't read this OFX/QFX file ({error}).") from error
    return [
        Row(txn.date.date(), _to_cents(str(txn.amount)), (txn.payee or txn.memo or "").strip())
        for account in ofx.accounts
        for txn in account.statement.transactions
    ]


def _parse_csv(content: bytes) -> list[Row]:
    text = content.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    headers = {header.strip().lower(): header for header in reader.fieldnames or []}

    date_col = _pick(headers, DATE_COLUMNS)
    description_col = _pick(headers, DESCRIPTION_COLUMNS)
    amount_col = _pick(headers, AMOUNT_COLUMNS)
    debit_col, credit_col = _pick(headers, DEBIT_COLUMNS), _pick(headers, CREDIT_COLUMNS)
    if not (date_col and description_col and (amount_col or debit_col)):
        found = ", ".join(reader.fieldnames or []) or "none"
        raise UnreadableExport(f"Need date, description and amount columns. Found: {found}.")

    rows = []
    for line_number, record in enumerate(reader, start=2):
        if not any((value or "").strip() for value in record.values()):
            continue
        try:
            if amount_col:
                cents = _to_cents(record[amount_col])
            else:
                credit = _to_cents(record[credit_col] or "0") if credit_col else 0
                cents = credit - abs(_to_cents(record[debit_col] or "0"))
            rows.append(Row(_to_date(record[date_col]), cents, record[description_col].strip()))
        except (ValueError, InvalidOperation) as error:
            raise UnreadableExport(f"Line {line_number}: {error}") from error

    # Card exports often list purchases as positive. Most rows in any export are spending,
    # so if most amounts are positive, flip the sign to the "negative = money out" convention.
    if amount_col and sum(row.amount_cents > 0 for row in rows) > len(rows) / 2:
        rows = [Row(row.posted_on, -row.amount_cents, row.description) for row in rows]
    return rows


def _pick(headers: dict[str, str], candidates: list[str]) -> str | None:
    return next((headers[name] for name in candidates if name in headers), None)


def _to_cents(raw: str) -> int:
    text = raw.strip().replace("$", "").replace(",", "")
    if text.startswith("(") and text.endswith(")"):
        text = "-" + text[1:-1]
    return int((Decimal(text) * 100).quantize(Decimal(1)))


def _to_date(raw: str) -> date:
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(raw.strip(), fmt).date()
        except ValueError:
            continue
    raise ValueError(f"unrecognized date {raw!r}")
