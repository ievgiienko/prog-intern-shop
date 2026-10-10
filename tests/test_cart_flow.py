from decimal import Decimal

import pytest
from django.urls import reverse

from catalog.models import ProductVariant
from operations.models import Inventory
from sales.models import Cart, Discount, DiscountRedemption, Order

pytestmark = pytest.mark.django_db


def _variant(catalog, sku):
    return ProductVariant.objects.get(sku=f"{sku}-STD")


CARD = {"holder": "Alice Example", "number": "4242 4242 4242 4242", "expiry": "12/39", "cvc": "123"}


def test_guest_can_fill_a_cart_without_logging_in(client, catalog):
    response = client.post(reverse("cart_add"), {"variant": _variant(catalog, "P1").id, "quantity": 1})
    assert response.status_code == 302 and response["Location"] == reverse("cart_detail")
    page = client.get(reverse("cart_detail"))
    assert page.status_code == 200 and page.context["cart"].is_guest and page.context["cart"].items.count() == 1


def test_full_purchase_journey(client, catalog, customer):
    client.force_login(customer)
    variant = _variant(catalog, "P1")
    assert client.post(reverse("cart_add"), {"variant": variant.id, "quantity": 2}).status_code == 302
    cart_page = client.get(reverse("cart_detail"))
    assert cart_page.context["totals"]["subtotal"] == Decimal("119.80")
    # quantity beyond stock is rejected with a friendly message
    client.post(reverse("cart_update"), {"item": cart_page.context["totals"]["items"][0].id, "quantity": 50}, follow=True)
    assert Cart.objects.get(customer=customer, active=True).items.get().quantity == 2
    checkout = client.post(reverse("checkout"), {
        "shipping_name": "Alice Example", "contact_email": "alice@example.test", "shipping_address": "Calle Mayor 1",
        "shipping_city": "Madrid", "shipping_postal_code": "28013", "shipping_country": "Spain"})
    order = Order.objects.get(customer=customer)
    assert checkout.status_code == 302 and checkout["Location"] == reverse("order_detail", args=[order.id])
    assert order.status == "PENDING_PAYMENT" and order.shipping_city == "Madrid"
    assert Inventory.objects.get(variant=variant).reserved == 2
    assert not Cart.objects.get(customer=customer, pk=order.customer.carts.first().pk).active
    # declined card keeps the order open, approved card confirms it
    client.post(reverse("order_pay", args=[order.id]), {**CARD, "number": "4000 0000 0000 0002"})
    order.refresh_from_db()
    assert order.status == "PENDING_PAYMENT" and order.payments.filter(status="FAILED").count() == 1
    bad = client.post(reverse("order_pay", args=[order.id]), {**CARD, "number": "1234"})
    assert bad.status_code == 400 and order.payments.count() == 1
    client.post(reverse("order_pay", args=[order.id]), CARD)
    order.refresh_from_db()
    assert order.status == "PAID" and order.payments.get(status="SUCCEEDED").card_last4 == "4242"
    assert "#%d" % order.id in client.get(reverse("order_list")).content.decode()


def test_checkout_validation_and_empty_cart(client, catalog, customer):
    client.force_login(customer)
    assert client.get(reverse("checkout")).status_code == 302  # empty cart -> redirected
    client.post(reverse("cart_add"), {"variant": _variant(catalog, "P1").id, "quantity": 1})
    response = client.post(reverse("checkout"), {"shipping_name": ""})
    assert response.status_code == 200 and response.context["form"].errors
    assert Order.objects.count() == 0


def test_discount_code_is_applied_and_usage_counted(client, catalog, customer):
    from django.utils import timezone
    Discount.objects.create(code="WELCOME10", kind="PERCENT", value=10, minimum_cart=20, usage_limit=5,
                            starts_at=timezone.now() - timezone.timedelta(days=1), ends_at=timezone.now() + timezone.timedelta(days=1))
    client.force_login(customer)
    client.post(reverse("cart_add"), {"variant": _variant(catalog, "P3").id, "quantity": 1})
    client.post(reverse("cart_apply_discount"), {"code": "welcome10"})
    totals = client.get(reverse("cart_detail")).context["totals"]
    assert totals["discount"] == Decimal("3.55")
    bad = client.post(reverse("cart_apply_discount"), {"code": "NOPE"}, follow=True)
    assert "not valid" in bad.content.decode()
    client.post(reverse("checkout"), {"shipping_name": "A", "contact_email": "a@example.test", "shipping_address": "x",
                                       "shipping_city": "y", "shipping_postal_code": "1", "shipping_country": "Spain"})
    order = Order.objects.get()

    assert order.discount_amount == Decimal("3.55")
    assert order.discount.code == "WELCOME10"
    assert order.coupon_code == "WELCOME10"
    assert Discount.objects.get().times_used == 1

    redemption = DiscountRedemption.objects.get(order=order)
    assert redemption.customer == customer
    assert redemption.code == "WELCOME10"
    assert redemption.amount == Decimal("3.55")


def test_customer_can_cancel_unpaid_order_and_stock_is_released(client, catalog, customer):
    client.force_login(customer)
    variant = _variant(catalog, "P1")
    client.post(reverse("cart_add"), {"variant": variant.id, "quantity": 3})
    client.post(reverse("checkout"), {"shipping_name": "A", "contact_email": "a@example.test", "shipping_address": "x",
                                       "shipping_city": "y", "shipping_postal_code": "1", "shipping_country": "Spain"})
    order = Order.objects.get()
    assert Inventory.objects.get(variant=variant).reserved == 3
    client.post(reverse("order_cancel", args=[order.id]))
    order.refresh_from_db()
    assert order.status == "CANCELLED" and Inventory.objects.get(variant=variant).reserved == 0


def test_orders_are_private(client, catalog, customer, staff):
    from django.contrib.auth.models import User
    other = User.objects.create_user("bob", password="x")
    order = Order.objects.create(customer=other, subtotal=1, total=1)
    client.force_login(customer)
    assert client.get(reverse("order_detail", args=[order.id])).status_code == 404
    client.force_login(staff)
    assert client.get(reverse("order_detail", args=[order.id])).status_code == 200


def test_registration_creates_profile_and_logs_in(client, db):
    response = client.post(reverse("register"), {
        "username": "newbie", "email": "new@example.test", "password1": "Tr1cky-password!", "password2": "Tr1cky-password!",
        "country": "Spain", "city": "Valencia"})
    assert response.status_code == 302
    profile = client.get(reverse("profile"))
    assert profile.status_code == 200 and profile.context["user"].customer_profile.city == "Valencia"
    assert profile.context["user"].groups.filter(name="Customer").exists()
