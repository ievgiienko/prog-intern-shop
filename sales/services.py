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

from .models import Cart, CartItem, Discount, Order, OrderHistory, OrderItem, Payment, DiscountRedemption
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


def discount_is_applicable(discount, subtotal, customer=None, now=None):
    """
    Check all customer-independent and customer-dependent discount rules.

    A guest cart may not have a customer yet, so first-order/per-customer
    checks are deferred to checkout when the guest identity is known.
    """
    now = now or timezone.now()

    if not discount.active:
        return False

    if not (discount.starts_at <= now <= discount.ends_at):
        return False

    if subtotal < discount.minimum_cart:
        return False

    if discount.times_used >= discount.usage_limit:
        return False

    if discount.kind == Discount.Kind.FREE_SHIPPING:
        return False

    if customer is not None:
        redemptions = DiscountRedemption.objects.filter(
            discount=discount,
            customer=customer,
        )

        if redemptions.count() >= discount.max_per_customer:
            return False

        if discount.first_order_only and Order.objects.filter(customer=customer).exists():
            return False

    return True

def validate_discount_for_customer(discount, subtotal, customer):
    """
    Raise a customer-facing error explaining why a discount cannot be used.

    This is the single validation path used by both apply-code and checkout.
    """
    now = timezone.now()

    if not discount.active:
        raise ValidationError("This discount code is not valid for your cart")

    if not (discount.starts_at <= now <= discount.ends_at):
        raise ValidationError("This discount code is not valid for your cart")

    if subtotal < discount.minimum_cart:
        raise ValidationError(
            f"This discount requires a minimum cart value of {discount.minimum_cart}"
        )

    if discount.times_used >= discount.usage_limit:
        raise ValidationError("This discount code has reached its usage limit")

    if discount.kind == Discount.Kind.FREE_SHIPPING:
        raise ValidationError(
            "This discount code offers free shipping, but free shipping is not available."
        )

    redemptions = DiscountRedemption.objects.filter(
        discount=discount,
        customer=customer,
    )

    if redemptions.count() >= discount.max_per_customer:
        raise ValidationError(
            "You have already used this discount code the maximum number of times."
        )

    if discount.first_order_only and Order.objects.filter(customer=customer).exists():
        raise ValidationError(
            "This discount code is available only on your first order."
        )

    return True



def apply_discount_code(cart, code):
    code = (code or "").strip().upper()

    if not code:
        cart.discount = None
        cart.save(update_fields=["discount", "updated_at"])
        return None

    discount = Discount.objects.filter(code__iexact=code).first()
    if discount is None:
        raise ValidationError("This discount code is not valid for your cart")

    customer = cart.customer

    if not discount_is_applicable(
        discount,
        cart.subtotal,
        customer=customer,
    ):
        if discount.kind == Discount.Kind.FREE_SHIPPING:
            raise ValidationError(
                "This discount code offers free shipping, but free shipping is not available."
            )

        if customer is not None:
            validate_discount_for_customer(
                discount,
                cart.subtotal,
                customer,
            )

        raise ValidationError("This discount code is not valid for your cart")

    # Stacking policy:
    # a cart can contain one discount only; applying another code replaces it.
    cart.discount = discount
    cart.save(update_fields=["discount", "updated_at"])
    return discount


def discount_amount_for(cart, subtotal, customer=None):
    discount = cart.discount
    customer = customer if customer is not None else cart.customer

    if not discount or not discount_is_applicable(
        discount,
        subtotal,
        customer=customer,
    ):
        return Decimal("0")

    amount = (
        discount.value
        if discount.kind == Discount.Kind.FIXED
        else subtotal * discount.value / 100
    )

    return min(amount, subtotal).quantize(Decimal("0.01"))


def cart_totals(cart):
    """Consumer prices are VAT-inclusive (EU retail): ``tax`` is the VAT *contained* in the total, not added on top."""
    items = list(cart.items.select_related("variant__product"))
    subtotal = sum((i.variant.effective_price * i.quantity for i in items), Decimal("0"))

    discount = discount_amount_for(
        cart,
        subtotal,
        customer=cart.customer,
    )

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
    """
    Turn an active cart into a PENDING_PAYMENT order.

    Discount eligibility is checked again here because a cart can become
    stale after the code was originally applied.
    """
    shipping = shipping or {}

    if not cart.active:
        raise ValidationError("This cart has already been checked out")

    items = list(
        cart.items.select_related("variant__product")
    )

    if not items:
        raise ValidationError("Cart is empty")

    for item in items:
        if not (item.variant.active and item.variant.product.is_visible):
            raise ValidationError(
                f"{item.variant.product.name} is no longer available"
            )

    # Resolve the real customer before validating customer-specific discount
    # rules. Guest carts are assigned their guest User here.
    customer = cart.customer

    if customer is None:
        email = shipping.get("contact_email", "")
        if not email:
            raise ValidationError({
                "contact_email": "An e-mail address is required for guest checkout"
            })

        customer = get_or_create_guest_user(
            email,
            shipping.get("shipping_name", ""),
        )

    subtotal = sum(
        (item.variant.effective_price * item.quantity for item in items),
        Decimal("0"),
    )

    discount = cart.discount

    # Lock the discount row while consuming its global quota. This prevents
    # concurrent checkouts from both consuming the same final slot.
    if discount is not None:
        discount = Discount.objects.select_for_update().get(pk=discount.pk)

        validate_discount_for_customer(
            discount,
            subtotal,
            customer,
        )

        discount_amount = discount_amount_for(
            cart,
            subtotal,
            customer=customer,
        )
    else:
        discount_amount = Decimal("0")

    total = subtotal - discount_amount

    tax = Decimal("0")
    for item in items:
        gross = item.variant.effective_price * item.quantity

        if subtotal:
            gross -= discount_amount * gross / subtotal

        rate = item.variant.product.tax_rate / 100
        tax += gross - gross / (1 + rate)

    tax = tax.quantize(Decimal("0.01"))

    plan = allocate(items)
    primary_warehouse = Counter(
        warehouse for _, warehouse, _ in plan
    ).most_common(1)[0][0]

    profile = getattr(customer, "customer_profile", None)

    order = Order.objects.create(
        customer=customer,
        warehouse_id=primary_warehouse,
        status="PENDING_PAYMENT",
        subtotal=subtotal,
        discount_amount=discount_amount,
        tax_amount=tax,
        total=total.quantize(Decimal("0.01")),
        fraud_score=risk_score(customer, total),
        source=getattr(profile, "acquisition_source", "direct"),
        discount=discount,
        coupon_code=discount.code if discount else "",
        **{k: shipping.get(k, "") for k in SHIPPING_FIELDS},
    )

    for item, warehouse_id, quantity in plan:
        product = item.variant.product

        reserve(
            item.variant_id,
            warehouse_id,
            quantity,
            "ORDER",
            str(order.id),
        )

        OrderItem.objects.create(
            order=order,
            variant=item.variant,
            warehouse_id=warehouse_id,
            product_name=product.name,
            sku=item.variant.sku,
            quantity=quantity,
            unit_price=item.variant.effective_price,
            unit_cost=product.purchase_cost,
            tax_rate=product.tax_rate,
        )

    if discount is not None:
        Discount.objects.filter(pk=discount.pk).update(
            times_used=F("times_used") + 1
        )

        DiscountRedemption.objects.create(
            discount=discount,
            order=order,
            customer=customer,
            code=discount.code,
            amount=discount_amount,
            currency=order.currency,
            created_at=timezone.now()
        )

    note = (
        "Split across warehouses"
        if len({w for _, w, _ in plan}) > 1
        else ""
    )

    OrderHistory.objects.create(
        order=order,
        from_status="NEW",
        to_status="PENDING_PAYMENT",
        note=note,
    )

    cart.active = False

    if cart.customer_id is None:
        cart.customer = customer

    cart.save(
        update_fields=["active", "customer", "updated_at"]
    )

    return order


@transaction.atomic
def transition(order, new_status, note=""):
    old = order.status

    if new_status not in TRANSITIONS.get(old, set()):
        raise ValidationError(
            f"Invalid transition {old} -> {new_status}"
        )

    if new_status == "CANCELLED":
        for item in order.items.all():
            release(
                item.variant_id,
                item.warehouse_id or order.warehouse_id,
                item.quantity,
                "ORDER_CANCEL",
                str(order.id),
            )

        # A cancellation restores the campaign quota consumed by this order.
        # The redemption row remains as historical evidence that the code
        # was actually used by this order.
        if order.discount_id and order.discount_amount > 0:
            Discount.objects.filter(pk=order.discount_id).update(
                times_used=F("times_used") - 1
            )

    if new_status == "SHIPPED":
        for item in order.items.all():
            ship(
                item.variant_id,
                item.warehouse_id or order.warehouse_id,
                item.quantity,
                "ORDER",
                str(order.id),
            )

    order.status = new_status
    order.save(update_fields=["status", "updated_at"])

    OrderHistory.objects.create(
        order=order,
        from_status=old,
        to_status=new_status,
        note=note,
    )

    if new_status == "PAID":
        from .tasks import send_order_confirmation

        transaction.on_commit(
            lambda: send_order_confirmation.delay(order.id)
        )

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
        transition(order, "CANCELLED", note=f"Payment not received within {max_age_hours}h")
        expired += 1
    return expired
