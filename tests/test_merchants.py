import pytest

from app.merchants import display_name, merchant_key


@pytest.mark.parametrize(
    "description, expected",
    [
        ("NETFLIX.COM 866-579-7172 CA", "NETFLIX"),
        ("POS PURCHASE NETFLIX.COM 11/02 #4411", "NETFLIX"),
        ("SQ *BLUE BOTTLE COFFEE #0123 OAKLAND CA", "BLUE BOTTLE COFFEE"),
        ("PAYPAL *HULU 402-935-7733 CA", "HULU"),
        ("POS PURCHASE PAYPAL *HULU 4029357733", "HULU"),
        ("ACH DEBIT ACME GYM MEMBERSHIP PPD ID: 99812", "ACME GYM MEMBERSHIP"),
        ("Recurring Payment authorized on 10/14 Spotify USA", "SPOTIFY USA"),
    ],
)
def test_merchant_key_strips_bank_noise(description, expected):
    assert merchant_key(description) == expected


def test_same_merchant_with_different_reference_numbers_groups_together():
    assert merchant_key("NETFLIX.COM 1234 CA") == merchant_key("NETFLIX.COM 9876 CA")


def test_unrecognizable_description_falls_back_to_itself():
    assert merchant_key("12345") == "12345"


def test_display_name_is_title_case():
    assert display_name("IRONWORKS GYM") == "Ironworks Gym"
    assert display_name("APPDEV PRO ANNUAL") == "Appdev Pro Annual"
