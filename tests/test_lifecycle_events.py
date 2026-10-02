from datetime import date, timedelta

import pytest
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from catalog.models import Product, ProductStatusHistory, ProductVariant, Review
from sales.models import Cart, Order
from sales.services import checkout, expire_unpaid_orders, transition

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _last_status_change(product):
    return ProductStatusHistory.objects.filter(product=product).first()  # newest first (Meta.ordering)


def _pending_order(customer):
    cart = Cart.objects.create(customer=customer)
    cart.items.create(variant=ProductVariant.objects.get(sku="P1-STD"), quantity=1)
    return checkout(cart)  # PENDING_PAYMENT


def _pending_review(catalog, customer):
    return Review.objects.create(customer=customer, product=catalog["products"]["P1"], rating=5)


# ---------------------------------------------------------------------------
# 1 + 2. product status history, launched_at / discontinued_at
# ---------------------------------------------------------------------------

def test_new_product_gets_initial_history_and_launch_date(catalog):
    product = catalog["products"]["P1"]  # created ACTIVE by the fixture
    first = ProductStatusHistory.objects.filter(product=product).get()
    assert (first.from_status, first.to_status) == ("", "ACTIVE")
    assert product.launched_at == timezone.localdate()


def test_status_change_via_service_records_history_and_discontinued_at(catalog):
    product = catalog["products"]["P1"]
    product.status = Product.Status.DISCONTINUED
    product.save()
    change = _last_status_change(product)
    assert (change.from_status, change.to_status) == ("ACTIVE", "DISCONTINUED")
    assert product.discontinued_at == timezone.localdate()
    product.name = "Renamed"
    product.save()
    assert ProductStatusHistory.objects.filter(product=product).count() == 2  # no row without a status change


def test_status_change_via_admin_action_records_history(client, catalog, staff):
    product = catalog["products"]["P1"]
    client.force_login(staff)
    client.post(reverse("admin:catalog_product_changelist"),
                {"action": "mark_discontinued", "_selected_action": [product.id]})
    change = _last_status_change(product)
    assert (change.from_status, change.to_status) == ("ACTIVE", "DISCONTINUED")
    product.refresh_from_db()
    assert product.discontinued_at == timezone.localdate()


def test_status_change_via_api_records_history(catalog, staff):
    product = catalog["products"]["P1"]
    api = APIClient()
    api.force_authenticate(staff)
    response = api.patch(f"/api/catalog/products/{product.id}/", {"status": "DISCONTINUED"}, format="json")
    assert response.status_code == 200
    change = _last_status_change(product)
    assert (change.from_status, change.to_status) == ("ACTIVE", "DISCONTINUED")


def test_launched_at_is_set_once_and_reactivation_clears_discontinued_at(catalog):
    product = catalog["products"]["P4"]  # DRAFT in the fixture
    assert product.launched_at is None
    product.status = Product.Status.ACTIVE
    product.save()
    assert product.launched_at == timezone.localdate()
    Product.objects.filter(pk=product.pk).update(launched_at=date(2020, 1, 1))
    product.refresh_from_db()
    product.status = Product.Status.DISCONTINUED
    product.save()
    assert product.discontinued_at == timezone.localdate()
    product.status = Product.Status.ACTIVE
    product.save()
    assert product.launched_at == date(2020, 1, 1)  # never overwritten
    assert product.discontinued_at is None


# ---------------------------------------------------------------------------
# 3. review moderated_at
# ---------------------------------------------------------------------------

def test_review_moderated_at_set_when_leaving_pending_via_service(catalog, customer):
    review = _pending_review(catalog, customer)
    assert review.moderated_at is None
    review.comment = "edited while still pending"
    review.save()
    assert review.moderated_at is None
    review.status = Review.Status.APPROVED
    review.save()
    assert review.moderated_at is not None


@pytest.mark.parametrize("action,expected", [("approve", "APPROVED"), ("reject", "REJECTED")])
def test_review_moderated_at_set_via_admin_action(client, catalog, customer, staff, action, expected):
    review = _pending_review(catalog, customer)
    client.force_login(staff)
    client.post(reverse("admin:catalog_review_changelist"), {"action": action, "_selected_action": [review.id]})
    review.refresh_from_db()
    assert review.status == expected and review.moderated_at is not None


def test_review_moderated_at_is_not_overwritten(catalog, customer):
    review = _pending_review(catalog, customer)
    review.status = Review.Status.APPROVED
    review.save()
    stamp = review.moderated_at
    review.status = Review.Status.REJECTED
    review.save()
    assert review.moderated_at == stamp


# ---------------------------------------------------------------------------
# 4. order cancel_reason: every cancel path records the reason that fits it
# ---------------------------------------------------------------------------

def test_cancel_reason_via_service(catalog, customer):
    order = _pending_order(customer)
    transition(order, "CANCELLED", cancel_reason=Order.CancelReason.DUPLICATE)
    order.refresh_from_db()
    assert order.status == "CANCELLED" and order.cancel_reason == "DUPLICATE"


def test_cancel_reason_rejects_unknown_value(catalog, customer):
    from rest_framework.exceptions import ValidationError
    order = _pending_order(customer)
    with pytest.raises(ValidationError):
        transition(order, "CANCELLED", cancel_reason="BECAUSE")
    order.refresh_from_db()
    assert order.status == "PENDING_PAYMENT"


def test_cancel_reason_for_expired_unpaid_orders(catalog, customer):
    order = _pending_order(customer)
    Order.objects.filter(pk=order.pk).update(created_at=timezone.now() - timedelta(hours=5))
    assert expire_unpaid_orders(1) == 1
    order.refresh_from_db()
    assert order.status == "CANCELLED" and order.cancel_reason == "PAYMENT_TIMEOUT"


def test_cancel_reason_for_customer_cancel_via_api(catalog, customer):
    order = _pending_order(customer)
    api = APIClient()
    api.force_authenticate(customer)
    assert api.post(f"/api/sales/orders/{order.id}/cancel/").status_code == 200
    order.refresh_from_db()
    assert order.cancel_reason == "CUSTOMER_REQUEST"


def test_cancel_reason_for_customer_cancel_via_storefront(client, catalog, customer):
    order = _pending_order(customer)
    client.force_login(customer)
    client.post(reverse("order_cancel", args=[order.id]))
    order.refresh_from_db()
    assert order.status == "CANCELLED" and order.cancel_reason == "CUSTOMER_REQUEST"


def test_staff_can_state_the_reason_via_api(catalog, customer, staff):
    api = APIClient()
    api.force_authenticate(staff)
    first, second = _pending_order(customer), _pending_order(customer)
    assert api.post(f"/api/sales/orders/{first.id}/cancel/", {"cancel_reason": "SUSPECTED_FRAUD"}).status_code == 200
    assert api.post(f"/api/sales/orders/{second.id}/transition/",
                    {"status": "CANCELLED", "note": "x", "cancel_reason": "DUPLICATE"}).status_code == 200
    first.refresh_from_db()
    second.refresh_from_db()
    assert (first.cancel_reason, second.cancel_reason) == ("SUSPECTED_FRAUD", "DUPLICATE")
    third = _pending_order(customer)
    assert api.post(f"/api/sales/orders/{third.id}/cancel/", {"cancel_reason": "BECAUSE"}).status_code == 400


@pytest.mark.parametrize("action,expected", [
    ("cancel_customer_request", "CUSTOMER_REQUEST"),
    ("cancel_suspected_fraud", "SUSPECTED_FRAUD"),
    ("cancel_duplicate", "DUPLICATE"),
    ("cancel_out_of_stock", "OUT_OF_STOCK"),
    ("cancel_address_invalid", "ADDRESS_INVALID"),
])
def test_cancel_reason_via_admin_actions(client, catalog, customer, staff, action, expected):
    order = _pending_order(customer)
    client.force_login(staff)
    client.post(reverse("admin:sales_order_changelist"), {"action": action, "_selected_action": [order.id]}, follow=True)
    order.refresh_from_db()
    assert order.status == "CANCELLED" and order.cancel_reason == expected