import hashlib
from collections import Counter, defaultdict
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import F
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from catalog.models import ProductVariant
from operations.models import Inventory
from operations.services import release, reserve, ship

from .models import Cart, CartItem, Discount, Order, OrderHistory, OrderItem, Payment
from .payments import OutcomeGateway, get_gateway

TRANSITIONS = {
    "NEW": {"PENDING_PAYMENT", "CANCELLED"},
    "PENDING_PAYMENT": {"PAID", "CANCELLED"},
    "PAID": {"PROCESSING", "CANCELLED"},
    "PROCESSING": {"PACKED", "CANCELLED"},
    "PACKED": {"SHIPPED"},
    "SHIPPED": {"DELIVERED"},
    "DELIVERED": {"RETURNED"},
    "RETURNED": {"REFUNDED"},
}

SHIPPING_FIELDS = ("shipping_name", "shipping_address", "shipping_city", "shipping_postal_code",
                   "shipping_country", "contact_email", "contact_phone", "customer_note")
SESSION_CART_KEY = "cart_id"
SESSION_GUEST_ORDERS_KEY = "guest_orders"


def risk_score(customer, total):
    score = 30 if total > 1000 else 10 if total > 500 else 0
    recent_failures = Payment.objects.filter(
        order__customer=customer, status="FAILED",
        created_at__gte=timezone.now() - timezone.timedelta(days=1)).count()
    score += min(30, recent_failures * 10)
    return min(score, 100)


# ---------------------------------------------------------------------------
# Carts: one active cart per customer, or one guest cart per session
# ---------------------------------------------------------------------------

def get_active_cart(user, create=True):
    cart = Cart.objects.filter(customer=user, active=True).order_by("-id").first()
    if cart is None and create:
        cart = Cart.objects.create(customer=user)
    return cart


def get_cart_for_request(request, create=True):
    """Authenticated users get their customer cart; anonymous visitors get a cart remembered in the session."""
    if request.user.is_authenticated:
        return get_active_cart(request.user, create=create)
    cart_id = request.session.get(SESSION_CART_KEY)
    cart = Cart.objects.filter(pk=cart_id, customer__isnull=True, active=True).first() if cart_id else None
    if cart is None and create:
        cart = Cart.objects.create(customer=None)
        request.session[SESSION_CART_KEY] = cart.id
    return cart


@transaction.atomic
def merge_session_cart(request, user):
    """Move the guest cart's lines into the user's active cart after login (quantities are added, capped by stock)."""
    cart_id = request.session.pop(SESSION_CART_KEY, None)
    if not cart_id:
        return None
    guest = Cart.objects.filter(pk=cart_id, customer__isnull=True, active=True).prefetch_related("items__variant__product").first()
    if guest is None:
        return None
    target = get_active_cart(user)
    for item in guest.items.all():
        try:
            add_to_cart(target, item.variant, item.quantity)
        except ValidationError:
            existing = target.items.filter(variant=item.variant).first()
            available = item.variant.available_quantity
            if available > 0 and (existing is None or existing.quantity < available):
                add_to_cart(target, item.variant, available, replace=True)
    if guest.discount_id and not target.discount_id:
        target.discount_id = guest.discount_id
        target.save(update_fields=["discount", "updated_at"])
    guest.delete()
    return target


@transaction.atomic
def add_to_cart(cart, variant, quantity=1, replace=False):
    """Add (or set) a variant quantity after checking it is purchasable and available somewhere."""
    quantity = int(quantity)
    if quantity < 1:
        raise ValidationError("Quantity must be at least 1")
    if not isinstance(variant, ProductVariant):
        variant = ProductVariant.objects.select_related("product").get(pk=variant)
    if not (variant.active and variant.product.is_visible):
        raise ValidationError("This product is not available for purchase")
    item, created = CartItem.objects.select_for_update().get_or_create(
        cart=cart, variant=variant, defaults={"quantity": quantity})
    if not created:
        item.quantity = quantity if replace else item.quantity + quantity
    if item.quantity > variant.available_quantity:
        raise ValidationError(f"Only {variant.available_quantity} unit(s) of {variant.product.name} are available")
    item.full_clean()
    item.save()
    cart.save(update_fields=["updated_at"])
    return item


def discount_is_applicable(discount, subtotal, now=None):
    now = now or timezone.now()
    return (discount.active and discount.starts_at <= now <= discount.ends_at
            and subtotal >= discount.minimum_cart and discount.times_used < discount.usage_limit)


def apply_discount_code(cart, code):
    code = (code or "").strip().upper()
    if not code:
        cart.discount = None
        cart.save(update_fields=["discount", "updated_at"])
        return None
    discount = Discount.objects.filter(code__iexact=code).first()
    if discount is None or not discount_is_applicable(discount, cart.subtotal):
        raise ValidationError("This discount code is not valid for your cart")
    cart.discount = discount
    cart.save(update_fields=["discount", "updated_at"])
    return discount


def discount_amount_for(cart, subtotal):
    discount = cart.discount
    if not discount or not discount_is_applicable(discount, subtotal):
        return Decimal("0")
    amount = discount.value if discount.kind == "FIXED" else subtotal * discount.value / 100
    return min(amount, subtotal).quantize(Decimal("0.01"))


def cart_totals(cart):
    """Consumer prices are VAT-inclusive (EU retail): ``tax`` is the VAT *contained* in the total, not added on top."""
    items = list(cart.items.select_related("variant__product"))
    subtotal = sum((i.variant.effective_price * i.quantity for i in items), Decimal("0"))
    discount = discount_amount_for(cart, subtotal)
    total = subtotal - discount
    tax = Decimal("0")
    for i in items:
        gross = i.variant.effective_price * i.quantity
        if subtotal:
            gross -= discount * gross / subtotal  # spread the discount proportionally before extracting VAT
        rate = i.variant.product.tax_rate / 100
        tax += gross - gross / (1 + rate)
    return {"items": items, "subtotal": subtotal, "discount": discount, "tax": tax.quantize(Decimal("0.01")),
            "total": total.quantize(Decimal("0.01"))}


# ---------------------------------------------------------------------------
# Guest customers
# ---------------------------------------------------------------------------

def get_or_create_guest_user(email, name=""):
    """Guest orders still belong to a User row (analytics, ownership), but a password-less one keyed by e-mail."""
    from accounts.models import CustomerProfile

    User = get_user_model()
    email = email.strip().lower()
    username = f"guest-{hashlib.sha1(email.encode()).hexdigest()[:12]}"
    user, created = User.objects.get_or_create(username=username, defaults={"email": email, "first_name": name[:150]})
    if created:
        user.set_unusable_password()
        user.save(update_fields=["password"])
        CustomerProfile.objects.create(user=user, is_guest=True, acquisition_source="direct")
    return user


# ---------------------------------------------------------------------------
# Stock allocation across warehouses
# ---------------------------------------------------------------------------

def allocate(items):
    """Decide which warehouse ships each line.

    Policy: (1) if one warehouse can fulfil the whole cart, use the lowest-id such warehouse; (2) otherwise fulfil
    each line from the lowest-id warehouse holding enough stock; (3) if no single warehouse holds enough for a line,
    split that line across warehouses by descending availability. Returns a list of (item, warehouse_id, quantity).
    """
    variant_ids = [i.variant_id for i in items]
    stock = defaultdict(dict)
    for row in Inventory.objects.filter(variant_id__in=variant_ids, warehouse__active=True).order_by("warehouse_id"):
        stock[row.variant_id][row.warehouse_id] = row.physical - row.reserved
    candidates = None
    for item in items:
        ids = {w for w, qty in stock[item.variant_id].items() if qty >= item.quantity}
        candidates = ids if candidates is None else candidates & ids
    if candidates:
        warehouse_id = min(candidates)
        return [(item, warehouse_id, item.quantity) for item in items]
    plan = []
    for item in items:
        levels = stock[item.variant_id]
        total = sum(q for q in levels.values() if q > 0)
        if total < item.quantity:
            raise ValidationError(f"Insufficient stock for {item.variant.product.name}: {total} available")
        whole = [w for w, qty in sorted(levels.items()) if qty >= item.quantity]
        if whole:
            plan.append((item, whole[0], item.quantity))
            continue
        remaining = item.quantity
        for w, qty in sorted(levels.items(), key=lambda kv: (-kv[1], kv[0])):
            if remaining == 0 or qty <= 0:
                break
            take = min(qty, remaining)
            plan.append((item, w, take))
            remaining -= take
    return plan


# ---------------------------------------------------------------------------
# Order lifecycle
# ---------------------------------------------------------------------------

@transaction.atomic
def checkout(cart, shipping=None):
    """Turn an active cart into a PENDING_PAYMENT order, reserving stock line by line (possibly in several warehouses)."""
    shipping = shipping or {}
    if not cart.active:
        raise ValidationError("This cart has already been checked out")
    totals = cart_totals(cart)
    items = totals["items"]
    if not items:
        raise ValidationError("Cart is empty")
    for item in items:
        if not (item.variant.active and item.variant.product.is_visible):
            raise ValidationError(f"{item.variant.product.name} is no longer available")
    customer = cart.customer
    if customer is None:
        email = shipping.get("contact_email", "")
        if not email:
            raise ValidationError({"contact_email": "An e-mail address is required for guest checkout"})
        customer = get_or_create_guest_user(email, shipping.get("shipping_name", ""))
    plan = allocate(items)
    primary_warehouse = Counter(w for _, w, _ in plan).most_common(1)[0][0]
    profile = getattr(customer, "customer_profile", None)
    order = Order.objects.create(
        customer=customer, warehouse_id=primary_warehouse, status="PENDING_PAYMENT",
        subtotal=totals["subtotal"], discount_amount=totals["discount"], tax_amount=totals["tax"],
        total=totals["total"], fraud_score=risk_score(customer, totals["total"]),
        source=getattr(profile, "acquisition_source", "direct"),
        **{k: shipping.get(k, "") for k in SHIPPING_FIELDS},
    )
    for item, warehouse_id, quantity in plan:
        product = item.variant.product
        reserve(item.variant_id, warehouse_id, quantity, "ORDER", str(order.id))
        OrderItem.objects.create(
            order=order, variant=item.variant, warehouse_id=warehouse_id, product_name=product.name,
            sku=item.variant.sku, quantity=quantity, unit_price=item.variant.effective_price,
            unit_cost=product.purchase_cost, tax_rate=product.tax_rate)
    if totals["discount"] > 0 and cart.discount_id:
        Discount.objects.filter(pk=cart.discount_id).update(times_used=F("times_used") + 1)
    note = "Split across warehouses" if len({w for _, w, _ in plan}) > 1 else ""
    OrderHistory.objects.create(order=order, from_status="NEW", to_status="PENDING_PAYMENT", note=note)
    cart.active = False
    if cart.customer_id is None:
        cart.customer = customer  # keep the guest cart attached to the guest user for traceability
    cart.save(update_fields=["active", "customer", "updated_at"])
    return order


@transaction.atomic
def transition(order, new_status, note="", cancel_reason=""):
    old = order.status
    if new_status not in TRANSITIONS.get(old, set()):
        raise ValidationError(f"Invalid transition {old} -> {new_status}")
    update_fields = ["status", "updated_at"]
    if new_status == "CANCELLED":
        if cancel_reason and cancel_reason not in Order.CancelReason.values:
            raise ValidationError({"cancel_reason": "Unknown cancel reason"})
        order.cancel_reason = cancel_reason
        update_fields.append("cancel_reason")
        for item in order.items.all():
            release(item.variant_id, item.warehouse_id or order.warehouse_id, item.quantity, "ORDER_CANCEL", str(order.id))
    if new_status == "SHIPPED":
        for item in order.items.all():
            ship(item.variant_id, item.warehouse_id or order.warehouse_id, item.quantity, "ORDER", str(order.id))
    order.status = new_status
    order.save(update_fields=update_fields)
    OrderHistory.objects.create(order=order, from_status=old, to_status=new_status, note=note)
    if new_status == "PAID":
        from .tasks import send_order_confirmation
        transaction.on_commit(lambda: send_order_confirmation.delay(order.id))
    return order


@transaction.atomic
def charge(order, card, gateway=None):
    """Charge an order through the configured gateway and record the outcome as a Payment."""
    if order.status != "PENDING_PAYMENT":
        raise ValidationError(f"Order #{order.id} is not awaiting payment")
    gateway = gateway or get_gateway()
    result = gateway.charge(order, card)
    payment = Payment.objects.create(order=order, amount=order.total, status=result.status, reference=result.reference,
                                     gateway=gateway.name, card_last4=result.card_last4, message=result.message)
    if result.succeeded:
        transition(order, "PAID", note=f"Payment {payment.reference[:12]} via {gateway.name}")
    else:
        OrderHistory.objects.create(order=order, from_status=order.status, to_status=order.status,
                                    note=f"Payment {result.status.lower()} ({result.message})")
    return payment


def pay(order, succeed=True):
    """Backwards-compatible helper used by the simulator and the API: outcome decided by the caller."""
    return charge(order, card={}, gateway=OutcomeGateway(succeed))


@transaction.atomic
def confirm_payment(reference, status, message=""):
    """Webhook entry point for asynchronous gateways: settle a PENDING payment (idempotent)."""
    payment = Payment.objects.select_for_update().select_related("order").filter(reference=reference).first()
    if payment is None:
        raise ValidationError("Unknown payment reference")
    if payment.status != "PENDING":
        return payment
    if status not in {"SUCCEEDED", "FAILED"}:
        raise ValidationError("Status must be SUCCEEDED or FAILED")
    payment.status, payment.message = status, message or payment.message
    payment.save(update_fields=["status", "message"])
    order = Order.objects.select_for_update().get(pk=payment.order_id)
    if status == "SUCCEEDED" and order.status == "PENDING_PAYMENT":
        transition(order, "PAID", note=f"Payment {reference[:12]} confirmed by webhook")
    elif status == "FAILED":
        OrderHistory.objects.create(order=order, from_status=order.status, to_status=order.status,
                                    note=f"Payment failed ({message or 'webhook'})")
    payment.order = order  # expose the updated status to callers
    return payment


@transaction.atomic
def expire_unpaid_orders(max_age_hours):
    """Cancel orders that stayed PENDING_PAYMENT for too long so their reserved stock returns to the shelf."""
    cutoff = timezone.now() - timezone.timedelta(hours=max_age_hours)
    expired = 0
    for order in Order.objects.filter(status="PENDING_PAYMENT", created_at__lt=cutoff).select_for_update():
        transition(order, "CANCELLED", note=f"Payment not received within {max_age_hours}h",
                   cancel_reason=Order.CancelReason.PAYMENT_TIMEOUT)
        expired += 1
    return expired
