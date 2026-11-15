from collections import Counter
from datetime import date

from app.mail import Sender, propose


def sender(category="PROMOTIONS", subjects=("Big sale this weekend",), messages=10, unsubscribe=True,
           address="news@shop.example", name="Shop") -> Sender:
    return Sender(address=address, name=name, messages=messages, last_seen=date(2026, 11, 1),
                  categories=Counter({category: messages}), subjects=list(subjects),
                  unsubscribe_url="https://shop.example/u" if unsubscribe else None,
                  one_click=unsubscribe, has_list_unsubscribe=unsubscribe)


def test_marketing_with_an_unsubscribe_link_is_unsubscribed():
    assert propose(sender(), set()) == "unsubscribe"


def test_bulk_mail_without_an_unsubscribe_link_is_spam():
    assert propose(sender(unsubscribe=False), set()) == "spam"


def test_a_few_promos_without_a_link_are_left_alone():
    assert propose(sender(unsubscribe=False, messages=2), set()) == "keep"


def test_money_and_security_mail_moves_to_the_new_address():
    assert propose(sender(category="UPDATES", subjects=["Your statement is ready"], unsubscribe=False), set()) == "move"
    assert propose(sender(category="UPDATES", subjects=["New sign-in to your account"]), set()) == "move"


def test_shopping_account_mail_is_kept_not_unsubscribed():
    # Order and shipping notices often carry an unsubscribe header too; unsubscribing would cut them off.
    assert propose(sender(category="UPDATES", subjects=["Your order has shipped"]), set()) == "keep"


def test_a_newsletter_in_updates_is_unsubscribed():
    assert propose(sender(category="UPDATES", subjects=["This week in gardening"]), set()) == "unsubscribe"


def test_people_are_kept():
    assert propose(sender(category="PERSONAL", subjects=["dinner friday?"], unsubscribe=False, messages=40), set()) == "keep"


def test_a_service_you_pay_for_moves_to_the_new_address():
    promo = sender(address="hello@mail.streamflix.com", name="Streamflix")
    assert propose(promo, {"Streamflix"}) == "move"
    assert propose(promo, {"Musico"}) == "unsubscribe"


def test_messages_group_by_sender_with_the_newest_details():
    from app.mail import Message, group
    messages = [
        Message("Shop <News@Shop.example>", "Old sale", date(2026, 1, 5), "PROMOTIONS"),
        Message("Shop Deals <news@shop.example>", "New sale", date(2026, 10, 5), "PROMOTIONS",
                "<mailto:u@shop.example>, <https://shop.example/u?id=1>", "List-Unsubscribe=One-Click"),
        Message("Sam <sam@example.com>", "hi", date(2026, 3, 1), "PERSONAL"),
    ]
    by_address = {s.address: s for s in group(messages)}
    shop, sam = by_address["news@shop.example"], by_address["sam@example.com"]
    assert (shop.address, shop.name, shop.messages) == ("news@shop.example", "Shop Deals", 2)
    assert shop.subjects == ["New sale", "Old sale"]
    assert shop.unsubscribe_url == "https://shop.example/u?id=1" and shop.one_click
    assert sam.messages == 1 and not sam.has_list_unsubscribe


def billing(category, *subjects) -> bool:
    return sender(category=category, subjects=subjects, unsubscribe=False).costs_money


def test_repeat_billing_emails_mean_a_sender_charges_you():
    assert billing("UPDATES", "Amazon Web Services Billing Statement Available", "Amazon Web Services Billing Statement Available")
    assert billing("UPDATES", "Auto Pay Payment Reminder", "Your payment was received")
    assert billing("PERSONAL", "Your Streamflix membership has been renewed", "Your receipt from Streamflix")
    assert billing("UPDATES", "Your Cloudly plan renews on Dec 1", "Invoice #1042 from Cloudly")
    assert billing("UPDATES", "Your free trial ends in 3 days", "You've been charged $9.99")


def test_marketing_and_one_offs_do_not():
    assert not billing("PROMOTIONS", "Reminder: your CinnaPack comes with $5", "Get $10 off your next order")
    assert not billing("PROMOTIONS", "Your receipt", "Your receipt")  # marketing tab: not a bill, whatever it says
    assert not billing("UPDATES", "Your receipt from Hardware Barn")  # a single purchase isn't recurring
    assert not billing("UPDATES", "Weekly digest: 5 subscription apps worth trying", "New: renew your style")
    assert not billing("UPDATES", "Your statement is ready", "Your statement is ready")  # a statement isn't a charge


def test_a_sender_billing_you_on_repeat_moves_to_the_new_address():
    streaming = sender(category="UPDATES", subjects=["Your membership has been renewed", "Your receipt"], unsubscribe=False)
    assert propose(streaming, set()) == "move"
