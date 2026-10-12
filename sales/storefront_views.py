import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from rest_framework.exceptions import ValidationError

from catalog.models import ProductVariant

from . import services
from .forms import AddToCartForm, CardPaymentForm, CheckoutForm, DiscountForm, UpdateCartItemForm
from .models import CartItem, Order
from .payments import TEST_CARDS, get_gateway
from .services import SESSION_GUEST_ORDERS_KEY


def _error_text(exc):
    detail = exc.detail
    if isinstance(detail, (list, tuple)):
        return " ".join(str(d) for d in detail)
    if isinstance(detail, dict):
        return " ".join(f"{v}" for v in detail.values())
    return str(detail)


def _safe_next(request, fallback):
    target = request.POST.get("next") or request.GET.get("next")
    if target and url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()}):
        return target
    return fallback


def _order_url(order):
    url = reverse("order_detail", args=[order.id])
    return url if order.customer.has_usable_password() else f"{url}?token={order.access_token}"


# ---------------------------------------------------------------------------
# Cart (works for guests and customers)
# ---------------------------------------------------------------------------

def cart_detail(request):
    cart = services.get_cart_for_request(request)
    totals = services.cart_totals(cart)
    return render(request, "sales/cart.html", {"cart": cart, "totals": totals,
                                               "discount_form": DiscountForm(initial={"code": cart.discount.code if cart.discount else ""})})


@require_POST
def cart_add(request):
    is_ajax = request.headers.get("X-Requested-With") =="XMLHttpRequest"
    form = AddToCartForm(request.POST)
    fallback = reverse("cart_detail")
    if not form.is_valid():
        messages.error(request, "Please choose a valid quantity.")
        return redirect(_safe_next(request, fallback))
    variant = get_object_or_404(ProductVariant.objects.select_related("product"), pk=form.cleaned_data["variant"])
    try:
        services.add_to_cart(services.get_cart_for_request(request), variant, form.cleaned_data["quantity"])
    except ValidationError as exc:
        messages.error(request, _error_text(exc))
        if is_ajax: 
            return JsonResponse({"error": _error_text(exc)}, status=400)
        return redirect(_safe_next(request, variant.product.get_absolute_url()))
    if is_ajax: cart = services.get_cart_for_request(request)
    totals = services.cart_totals(cart)
    items = [{ "id": item.id, "name": item.variant.product.name, "quantity": item.quantity,}
             for item in cart.items.select_related("variant__product").all()]
    return JsonResponse({"cart_count": cart.item_count, "subtotal": str(totals["subtotal"]), "items": items, "total": str(totals["total"])}) 
    messages.success(request, f"Added {variant.product.name} to your cart.")
    return redirect(_safe_next(request, fallback))



def _own_item(request, item_id):
    cart = services.get_cart_for_request(request, create=False)
    if cart is None:
        raise Http404
    return get_object_or_404(CartItem.objects.select_related("variant__product"), pk=item_id, cart=cart)


@require_POST
def cart_update(request):
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    form = UpdateCartItemForm(request.POST)
    if form.is_valid():
        item = _own_item(request, form.cleaned_data["item"])
        quantity = form.cleaned_data["quantity"]
        if quantity == 0:
            item.delete()
            messages.info(request, "Item removed.")
        else:
            try:
                services.add_to_cart(item.cart, item.variant, quantity, replace=True)
                if not is_ajax:
                 messages.success(request, "Cart updated.")
            except ValidationError as exc:
                messages.error(request, _error_text(exc))
                if is_ajax:
                    return JsonResponse({"error":_error_text}, status=400)
    else:
        messages.error(request, "Invalid quantity.")
    if is_ajax: 
        cart = services.get_cart_for_request(request)
        totals = services.cart_totals(cart)
        items = [{ "id": item.id, "name": item.variant.product.name, "quantity": item.quantity} 
                 for item in cart.items.select_related("variant__product").all()]
        return JsonResponse({"cart_count": cart.item_count, "subtotal": str(totals["subtotal"]), "total": str(totals["total"]), "items": items,})
    return redirect("cart_detail")


@require_POST
def cart_remove(request, item_id):
    _own_item(request, item_id).delete()
    messages.info(request, "Item removed from your cart.")
    return redirect("cart_detail")


@require_POST
def cart_apply_discount(request):
    form = DiscountForm(request.POST)
    if form.is_valid():
        try:
            discount = services.apply_discount_code(services.get_cart_for_request(request), form.cleaned_data["code"])
            messages.success(request, f"Discount {discount.code} applied." if discount else "Discount removed.")
        except ValidationError as exc:
            messages.error(request, _error_text(exc))
    return redirect("cart_detail")


# ---------------------------------------------------------------------------
# Checkout (guests supply an e-mail and get a tokenised order link)
# ---------------------------------------------------------------------------

def checkout(request):
    cart = services.get_cart_for_request(request)
    totals = services.cart_totals(cart)
    if not totals["items"]:
        messages.info(request, "Your cart is empty.")
        return redirect("product_list")
    user = request.user
    initial = {"shipping_country": "Spain"}
    if user.is_authenticated:
        profile = getattr(user, "customer_profile", None)
        last_order = Order.objects.filter(customer=user).exclude(shipping_address="").first()
        initial = {
            "shipping_name": (last_order.shipping_name if last_order else f"{user.first_name} {user.last_name}".strip()),
            "contact_email": (last_order.contact_email if last_order else user.email),
            "contact_phone": last_order.contact_phone if last_order else "",
            "shipping_address": last_order.shipping_address if last_order else "",
            "shipping_city": last_order.shipping_city if last_order else getattr(profile, "city", ""),
            "shipping_postal_code": last_order.shipping_postal_code if last_order else "",
            "shipping_country": last_order.shipping_country if last_order else (getattr(profile, "country", "") or "Spain"),
        }
    form = CheckoutForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        try:
            order = services.checkout(cart, shipping=form.cleaned_data)
        except ValidationError as exc:
            messages.error(request, _error_text(exc))
            return redirect("cart_detail")
        if not user.is_authenticated:
            guest_orders = request.session.get(SESSION_GUEST_ORDERS_KEY, [])
            request.session[SESSION_GUEST_ORDERS_KEY] = guest_orders + [order.id]
            request.session.pop(services.SESSION_CART_KEY, None)
        messages.success(request, f"Order #{order.id} created. Complete the payment to confirm it.")
        return redirect(_order_url(order))
    return render(request, "sales/checkout.html", {"form": form, "cart": cart, "totals": totals, "is_guest": not user.is_authenticated})


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------

def _accessible_order(request, order_id):
    """Staff: any order. Customer: own orders. Guest: orders from this session or opened with the access token."""
    qs = Order.objects.select_related("customer", "warehouse").prefetch_related("items__warehouse", "history", "payments")
    order = get_object_or_404(qs, pk=order_id)
    if request.user.is_staff or (request.user.is_authenticated and order.customer_id == request.user.id):
        return order
    token = request.GET.get("token") or request.POST.get("token")
    if token and str(order.access_token) == token:
        return order
    if order.id in request.session.get(SESSION_GUEST_ORDERS_KEY, []):
        return order
    raise Http404


@login_required
def order_list(request):
    orders = Order.objects.filter(customer=request.user).prefetch_related("items").order_by("-created_at")
    return render(request, "sales/order_list.html", {"orders": orders})


def order_detail(request, order_id):
    order = _accessible_order(request, order_id)
    form = CardPaymentForm(initial={"holder": order.shipping_name}) if order.can_pay else None
    return render(request, "sales/order_detail.html", {
        "order": order, "payment_form": form, "gateway": get_gateway().name, "test_cards": TEST_CARDS,
        "token": request.GET.get("token", ""), "is_guest_view": not request.user.is_authenticated})


@require_POST
def order_pay(request, order_id):
    order = _accessible_order(request, order_id)
    token = request.POST.get("token", "")
    back = reverse("order_detail", args=[order.id]) + (f"?token={token}" if token else "")
    form = CardPaymentForm(request.POST)
    if not form.is_valid():
        return render(request, "sales/order_detail.html", {
            "order": order, "payment_form": form, "gateway": get_gateway().name, "test_cards": TEST_CARDS,
            "token": token, "is_guest_view": not request.user.is_authenticated}, status=400)
    try:
        payment = services.charge(order, card=form.cleaned_data)
    except ValidationError as exc:
        messages.error(request, _error_text(exc))
        return redirect(back)
    if payment.status == "SUCCEEDED":
        messages.success(request, f"Payment received. Order #{order.id} is confirmed.")
    elif payment.status == "PENDING":
        messages.info(request, "Payment is being authorised. We will confirm it as soon as the provider answers.")
    else:
        messages.error(request, f"Payment declined: {payment.message}. You can try another card.")
    return redirect(back)


@require_POST
def order_cancel(request, order_id):
    order = _accessible_order(request, order_id)
    token = request.POST.get("token", "")
    back = reverse("order_detail", args=[order.id]) + (f"?token={token}" if token else "")
    if order.status not in {"NEW", "PENDING_PAYMENT", "PAID"} and not request.user.is_staff:
        messages.error(request, "This order can no longer be cancelled online. Please contact support.")
        return redirect(back)
    try:
        services.transition(order, "CANCELLED", note="Cancelled by staff" if request.user.is_staff else "Cancelled by customer")
        messages.info(request, f"Order #{order.id} was cancelled and the stock was released.")
    except ValidationError as exc:
        messages.error(request, _error_text(exc))
    return redirect(back)


# ---------------------------------------------------------------------------
# Payment provider webhook (asynchronous confirmation)
# ---------------------------------------------------------------------------

@csrf_exempt
@require_POST
def payment_webhook(request, gateway):
    provider = get_gateway()
    if gateway != provider.name or not provider.verify_webhook(request):
        return HttpResponseForbidden("invalid signature")
    try:
        payload = json.loads(request.body or b"{}")
        payment = services.confirm_payment(payload["reference"], payload["status"], payload.get("message", ""))
    except (KeyError, ValueError) as exc:
        return JsonResponse({"error": "invalid payload", "detail": str(exc)}, status=400)
    except ValidationError as exc:
        return JsonResponse({"error": _error_text(exc)}, status=400)
    return JsonResponse({"reference": payment.reference, "status": payment.status, "order": payment.order_id,
                         "order_status": payment.order.status})
