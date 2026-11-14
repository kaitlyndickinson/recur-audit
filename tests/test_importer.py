from datetime import date

import pytest

from app.importer import Row, UnreadableExport, parse

CARD_CSV = b"""Date,Description,Amount
11/02/2026,STREAMFLIX.COM 866-555-0100 CA,15.49
11/05/2026,CORNER CAFE #12,4.75
11/09/2026,ONLINE PAYMENT - THANK YOU,-120.00
"""

CHECKING_CSV = b"""\xef\xbb\xbfPosting Date,Payee,Debit,Credit
2026-11-01,ACH DEBIT ACME GYM PPD,"1,030.00",
2026-11-03,PAYROLL EXAMPLE CO,,2500.00
"""

OFX = b"""OFXHEADER:100
DATA:OFXSGML
VERSION:102
SECURITY:NONE
ENCODING:USASCII
CHARSET:1252
COMPRESSION:NONE
OLDFILEUID:NONE
NEWFILEUID:NONE

<OFX><SIGNONMSGSRSV1><SONRS><STATUS><CODE>0<SEVERITY>INFO</STATUS><DTSERVER>20261110<LANGUAGE>ENG</SONRS></SIGNONMSGSRSV1>
<BANKMSGSRSV1><STMTTRNRS><TRNUID>1<STATUS><CODE>0<SEVERITY>INFO</STATUS>
<STMTRS><CURDEF>USD<BANKACCTFROM><BANKID>000000000<ACCTID>0000<ACCTTYPE>CHECKING</BANKACCTFROM>
<BANKTRANLIST><DTSTART>20261101<DTEND>20261110
<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20261104<TRNAMT>-9.99<FITID>1<NAME>MUSICO SUBSCRIPTION</STMTTRN>
<STMTTRN><TRNTYPE>CREDIT<DTPOSTED>20261106<TRNAMT>50.00<FITID>2<NAME>REFUND EXAMPLE</STMTTRN>
</BANKTRANLIST><LEDGERBAL><BALAMT>100.00<DTASOF>20261110</LEDGERBAL></STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>
"""


def test_card_export_with_positive_purchases_is_flipped_to_money_out_negative():
    rows = parse("card.csv", CARD_CSV)
    assert rows[0] == Row(date(2026, 11, 2), -1549, "STREAMFLIX.COM 866-555-0100 CA")
    assert rows[2].amount_cents == 12000  # the card payment is money in


def test_debit_and_credit_columns_with_thousands_separator_and_bom():
    rows = parse("checking.csv", CHECKING_CSV)
    assert rows == [
        Row(date(2026, 11, 1), -103000, "ACH DEBIT ACME GYM PPD"),
        Row(date(2026, 11, 3), 250000, "PAYROLL EXAMPLE CO"),
    ]


def test_parenthesized_amounts_are_negative():
    rows = parse("x.csv", b"Date,Description,Amount\n2026-01-01,A,(12.50)\n2026-01-02,B,-3.00\n")
    assert [row.amount_cents for row in rows] == [-1250, -300]


def test_unknown_columns_say_what_was_found():
    with pytest.raises(UnreadableExport, match="Found: When, What"):
        parse("x.csv", b"When,What\n2026-01-01,A\n")


def test_bad_date_reports_the_line():
    with pytest.raises(UnreadableExport, match="Line 3"):
        parse("x.csv", b"Date,Description,Amount\n2026-01-01,A,-1\nyesterday,B,-2\n")


def test_ofx_transactions_keep_their_sign():
    rows = parse("bank.OFX", OFX)
    assert rows == [
        Row(date(2026, 11, 4), -999, "MUSICO SUBSCRIPTION"),
        Row(date(2026, 11, 6), 5000, "REFUND EXAMPLE"),
    ]
