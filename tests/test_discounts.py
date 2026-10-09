from decimal import Decimal

import pytest
from django.contrib.auth.models import User
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from sales.models import Cart, CartItem, Discount, DiscountRedemption, Order
from sales.services import apply_discount_code, checkout, transition


pytestmark = pytest.mark.django_db


def _active_discount(**kwargs):
    now = timezone.now()

    defaults = {
        "code": "TEST10",
        "kind": Discount.Kind.PERCENT,
        "value": Decimal("10"),
        "minimum_cart": Decimal("0"),
        "usage_limit": 100,
        "times_used": 0,
        "active": True,
        "starts_at": now - timezone.timedelta(days=1),
        "ends_at": now + timezone.timedelta(days=1),
    }

    defaults.update(kwargs)
    return Discount.objects.create(**defaults)


def _cart(customer, catalog, sku="P1", quantity=1):
    cart = Cart.objects.create(customer=customer)
    CartItem.objects.create(
        cart=cart,
        variant=catalog["products"][sku].variants.first(),
        quantity=quantity,
    )
    return cart


def _checkout(cart):
    return checkout(
        cart,
        shipping={
            "shipping_name": "Alice Example",
            "contact_email": cart.customer.email,
            "shipping_address": "Test Street 1",
            "shipping_city": "Madrid",
            "shipping_postal_code": "28013",
            "shipping_country": "Spain",
        },
    )


def test_first_order_only_allows_customer_without_previous_orders(customer, catalog):
    discount = _active_discount(
        code="FIRST10",
        first_order_only=True,
    )

    cart = _cart(customer, catalog)
    apply_discount_code(cart, discount.code)

    assert cart.discount == discount


def test_first_order_only_rejects_returning_customer(customer, catalog):
    discount = _active_discount(
        code="FIRST10",
        first_order_only=True,
    )

    Order.objects.create(
        customer=customer,
        subtotal=Decimal("50"),
        total=Decimal("50"),
    )

    cart = _cart(customer, catalog)

    with pytest.raises(
        ValidationError,
        match="available only on your first order",
    ):
        apply_discount_code(cart, discount.code)


def test_max_per_customer_rejects_code_at_apply_step(customer, catalog):
    discount = _active_discount(
        code="ONCE10",
        max_per_customer=1,
    )

    order = Order.objects.create(
        customer=customer,
        subtotal=Decimal("50"),
        total=Decimal("45"),
        discount=discount,
        coupon_code=discount.code,
        discount_amount=Decimal("5"),
    )

    DiscountRedemption.objects.create(
        discount=discount,
        order=order,
        customer=customer,
        code=discount.code,
        amount=Decimal("5"),
        currency="EUR",
        created_at=timezone.now(),
    )

    cart = _cart(customer, catalog)

    with pytest.raises(
        ValidationError,
        match="maximum number of times",
    ):
        apply_discount_code(cart, discount.code)


def test_max_per_customer_is_checked_again_at_checkout(customer, catalog):
    discount = _active_discount(
        code="STALE10",
        max_per_customer=1,
    )

    cart = _cart(customer, catalog)
    apply_discount_code(cart, discount.code)

    # The cart is now stale: the customer used the code elsewhere.
    previous_order = Order.objects.create(
        customer=customer,
        subtotal=Decimal("50"),
        total=Decimal("45"),
        discount=discount,
        coupon_code=discount.code,
        discount_amount=Decimal("5"),
    )

    DiscountRedemption.objects.create(
        discount=discount,
        order=previous_order,
        customer=customer,
        code=discount.code,
        amount=Decimal("5"),
        currency="EUR",
        created_at=timezone.now(),
    )

    with pytest.raises(
        ValidationError,
        match="maximum number of times",
    ):
        _checkout(cart)

    assert Order.objects.count() == 1
    discount.refresh_from_db()
    assert discount.times_used == 0


def test_free_shipping_is_rejected_explicitly(customer, catalog):
    discount = _active_discount(
        code="SHIPFREE",
        kind=Discount.Kind.FREE_SHIPPING,
        value=Decimal("0"),
    )

    cart = _cart(customer, catalog)

    with pytest.raises(
        ValidationError,
        match="free shipping is not available",
    ):
        apply_discount_code(cart, discount.code)


def test_checkout_creates_redemption(customer, catalog):
    discount = _active_discount(
        code="SAVE10",
        value=Decimal("10"),
    )

    cart = _cart(customer, catalog, sku="P1", quantity=1)
    apply_discount_code(cart, discount.code)

    order = _checkout(cart)

    redemption = DiscountRedemption.objects.get(
        discount=discount,
        order=order,
    )

    assert redemption.customer == customer
    assert redemption.code == "SAVE10"
    assert redemption.amount == Decimal("5.99")
    assert redemption.currency == order.currency

    order.refresh_from_db()
    discount.refresh_from_db()

    assert order.discount == discount
    assert order.coupon_code == "SAVE10"
    assert order.discount_amount == Decimal("5.99")
    assert discount.times_used == 1


def test_checkout_rechecks_first_order_only(customer, catalog):
    discount = _active_discount(
        code="WELCOME10",
        first_order_only=True,
    )

    cart = _cart(customer, catalog)
    apply_discount_code(cart, discount.code)

    # Customer becomes a returning customer after applying the code.
    Order.objects.create(
        customer=customer,
        subtotal=Decimal("50"),
        total=Decimal("50"),
    )

    with pytest.raises(
        ValidationError,
        match="available only on your first order",
    ):
        _checkout(cart)

    discount.refresh_from_db()
    assert discount.times_used == 0


def test_cancelled_discounted_order_restores_quota(customer, catalog):
    discount = _active_discount(
        code="CANCEL10",
        usage_limit=1,
    )

    cart = _cart(customer, catalog)
    apply_discount_code(cart, discount.code)

    order = _checkout(cart)

    discount.refresh_from_db()
    assert discount.times_used == 1
    assert DiscountRedemption.objects.filter(order=order).count() == 1

    transition(order, "CANCELLED")

    discount.refresh_from_db()

    assert order.status == "CANCELLED"
    assert discount.times_used == 0

    # Historical redemption remains.
    assert DiscountRedemption.objects.filter(order=order).count() == 1


def test_cancelled_order_restores_quota_only_once(customer, catalog):
    discount = _active_discount(
        code="CANCEL10",
        usage_limit=2,
    )

    cart = _cart(customer, catalog)
    apply_discount_code(cart, discount.code)

    order = _checkout(cart)

    transition(order, "CANCELLED")

    discount.refresh_from_db()
    assert discount.times_used == 0

    with pytest.raises(ValidationError, match="Invalid transition"):
        transition(order, "CANCELLED")

    discount.refresh_from_db()
    assert discount.times_used == 0


def test_usage_limit_is_enforced_at_checkout(customer, catalog):
    discount = _active_discount(
        code="LIMIT1",
        usage_limit=1,
    )

    cart = _cart(customer, catalog)
    apply_discount_code(cart, discount.code)

    # Another order consumes the only available campaign slot.
    other = User.objects.create_user(
        "other",
        email="other@example.test",
    )

    other_cart = _cart(other, catalog)
    apply_discount_code(other_cart, discount.code)
    _checkout(other_cart)

    discount.refresh_from_db()
    assert discount.times_used == 1

    with pytest.raises(
        ValidationError,
        match="reached its usage limit",
    ):
        _checkout(cart)

    assert Order.objects.filter(customer=customer).count() == 0


def test_applying_another_code_replaces_the_existing_code(customer, catalog):
    first = _active_discount(
        code="FIRST10",
        value=Decimal("10"),
    )

    second = _active_discount(
        code="SECOND20",
        value=Decimal("20"),
    )

    cart = _cart(customer, catalog)

    apply_discount_code(cart, first.code)
    assert cart.discount == first

    apply_discount_code(cart, second.code)

    cart.refresh_from_db()
    assert cart.discount == second