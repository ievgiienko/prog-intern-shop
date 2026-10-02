from django.db import transaction
from django.db.models import Count
from rest_framework import decorators, permissions, response, status, viewsets
from rest_framework.exceptions import PermissionDenied

from config.api import DatasetPagination, IsAnalyst

from .filters import (OrderFilter, OrderHistoryFilter, OrderItemFilter, PaymentFilter, RedemptionFilter, RefundFilter,
                      ReturnFilter, ShipmentFilter, SubscriptionFilter)
from .models import (Cart, CartItem, Discount, DiscountRedemption, Order, OrderHistory, OrderItem, Payment, Refund,
                     ReturnRequest, Shipment, Subscription)
from .serializers import (AddItemSerializer, CartSerializer, CheckoutSerializer, DiscountCodeSerializer,
                          DiscountRedemptionSerializer, DiscountSerializer, OrderHistorySerializer, OrderItemSerializer,
                          OrderSerializer, PaySerializer, PaymentSerializer, RefundSerializer, RemoveItemSerializer,
                          ReturnSerializer, ShipmentSerializer, SubscriptionSerializer, TransitionSerializer)
from .services import add_to_cart, apply_discount_code, charge, checkout, pay, transition


class OwnedMixin:
    """Customers see only their own rows; staff see everything.

    Endpoints that set ``analyst_readable`` also open their *read* side to the reporting roles, so an
    analyst can extract the data without being granted staff rights over the shop.
    """

    analyst_readable = False

    def get_queryset(self):
        qs = super().get_queryset()
        if getattr(self, "swagger_fake_view", False):
            return qs.none()
        if self.request.user.is_staff:
            return qs
        if (self.analyst_readable and self.request.method in permissions.SAFE_METHODS
                and IsAnalyst().has_permission(self.request, self)):
            return qs
        return qs.filter(customer=self.request.user)


class CartViewSet(OwnedMixin, viewsets.ModelViewSet):
    queryset = Cart.objects.prefetch_related("items__variant__product").select_related("discount").order_by("-id")
    serializer_class = CartSerializer
    filterset_fields = ["active"]

    def perform_create(self, serializer):
        serializer.save(customer=self.request.user)

    def _respond(self, cart):
        cart.refresh_from_db()
        return response.Response(CartSerializer(cart, context=self.get_serializer_context()).data)

    @decorators.action(detail=True, methods=["post"], serializer_class=AddItemSerializer)
    def add_item(self, request, pk=None):
        """Set the quantity of a variant in this cart (creates the line when missing)."""
        cart = self.get_object()
        data = AddItemSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        add_to_cart(cart, data.validated_data["variant"], data.validated_data["quantity"], replace=True)
        return self._respond(cart)

    @decorators.action(detail=True, methods=["post"], serializer_class=RemoveItemSerializer)
    def remove_item(self, request, pk=None):
        cart = self.get_object()
        data = RemoveItemSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        CartItem.objects.filter(cart=cart, variant=data.validated_data["variant"]).delete()
        return self._respond(cart)

    @decorators.action(detail=True, methods=["post"], serializer_class=DiscountCodeSerializer)
    def apply_discount(self, request, pk=None):
        cart = self.get_object()
        data = DiscountCodeSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        apply_discount_code(cart, data.validated_data["code"])
        return self._respond(cart)

    @decorators.action(detail=True, methods=["post"], serializer_class=CheckoutSerializer)
    def checkout(self, request, pk=None):
        data = CheckoutSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        order = checkout(self.get_object(), shipping=data.validated_data)
        return response.Response(OrderSerializer(order, context=self.get_serializer_context()).data, status=status.HTTP_201_CREATED)


class OrderViewSet(OwnedMixin, viewsets.ReadOnlyModelViewSet):
    analyst_readable = True
    queryset = (Order.objects.prefetch_related("items").select_related("customer", "warehouse")
                .annotate(item_count=Count("items")).order_by("-created_at"))
    serializer_class = OrderSerializer
    filterset_class = OrderFilter
    pagination_class = DatasetPagination
    ordering_fields = ["created_at", "paid_at", "delivered_at", "total", "subtotal", "discount_amount",
                       "status", "item_count", "fraud_score"]
    search_fields = ["id", "customer__username", "customer__email", "shipping_name", "contact_email",
                     "coupon_code", "utm_campaign"]

    @decorators.action(detail=True, methods=["get"], serializer_class=OrderItemSerializer)
    def items(self, request, pk=None):
        """The lines of one order."""
        rows = self.get_object().items.select_related("warehouse", "variant__product")
        return response.Response(OrderItemSerializer(rows, many=True).data)

    @decorators.action(detail=True, methods=["get"], serializer_class=OrderHistorySerializer)
    def history(self, request, pk=None):
        """Every status this order went through, oldest first."""
        return response.Response(OrderHistorySerializer(self.get_object().history.all(), many=True).data)

    @decorators.action(detail=True, methods=["get"], serializer_class=PaymentSerializer)
    def payments(self, request, pk=None):
        """All payment attempts, failed ones included."""
        return response.Response(PaymentSerializer(self.get_object().payments.all(), many=True).data)

    @decorators.action(detail=True, methods=["get"], serializer_class=RefundSerializer)
    def refunds(self, request, pk=None):
        return response.Response(RefundSerializer(self.get_object().refunds.all(), many=True).data)

    @decorators.action(detail=True, methods=["get"], serializer_class=ShipmentSerializer)
    def shipments(self, request, pk=None):
        return response.Response(ShipmentSerializer(self.get_object().shipments.all(), many=True).data)

    @decorators.action(detail=True, methods=["post"], serializer_class=PaySerializer)
    def pay(self, request, pk=None):
        data = PaySerializer(data=request.data)
        data.is_valid(raise_exception=True)
        card = data.validated_data
        if card.get("number"):
            payment = charge(self.get_object(), card=card)
        else:
            payment = pay(self.get_object(), card.get("success", True))
        return response.Response(PaymentSerializer(payment).data)

    @decorators.action(detail=True, methods=["post"], serializer_class=TransitionSerializer)
    def transition(self, request, pk=None):
        if not request.user.is_staff:
            raise PermissionDenied("Staff role required")
        data = TransitionSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        order = transition(self.get_object(), data.validated_data["status"], note=data.validated_data["note"],
                           cancel_reason=request.data.get("cancel_reason", ""))
        return response.Response(OrderSerializer(order, context=self.get_serializer_context()).data)

    @decorators.action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        """Customers may cancel before fulfilment starts; staff may cancel anything the lifecycle allows."""
        order = self.get_object()
        if not request.user.is_staff and order.status not in {"NEW", "PENDING_PAYMENT", "PAID"}:
            raise PermissionDenied("This order can no longer be cancelled online")
        reason = Order.CancelReason.CUSTOMER_REQUEST
        if request.user.is_staff:
            reason = request.data.get("cancel_reason") or reason  # staff may state the real reason
        with transaction.atomic():
            order = transition(order, "CANCELLED", note=f"Cancelled by {request.user.username}",
                               cancel_reason=reason)
        return response.Response(OrderSerializer(order, context=self.get_serializer_context()).data)


class ReturnViewSet(viewsets.ModelViewSet):
    serializer_class = ReturnSerializer
    filterset_class = ReturnFilter
    pagination_class = DatasetPagination
    ordering_fields = ["created_at", "resolved_at", "refund_amount", "quantity"]

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return ReturnRequest.objects.none()
        qs = ReturnRequest.objects.select_related("order_item__order").order_by("-created_at")
        if self.request.user.is_staff or (self.request.method in permissions.SAFE_METHODS
                                          and IsAnalyst().has_permission(self.request, self)):
            return qs
        return qs.filter(order_item__order__customer=self.request.user)


# ---------------------------------------------------------------------------
# Read-only fact tables. These are the grain analysts extract and join themselves;
# no ratio, rate or lifetime value is computed for them here.
# ---------------------------------------------------------------------------

class DatasetViewSet(viewsets.ReadOnlyModelViewSet):
    """Shared defaults for the reporting datasets: analyst role, large pages, sorting, search."""

    permission_classes = [IsAnalyst]
    pagination_class = DatasetPagination


class OrderItemViewSet(DatasetViewSet):
    """Order lines — the grain for product, category and basket analysis."""

    queryset = OrderItem.objects.select_related("order", "warehouse", "variant__product__category").order_by("-id")
    serializer_class = OrderItemSerializer
    filterset_class = OrderItemFilter
    ordering_fields = ["id", "quantity", "unit_price", "unit_cost", "discount_amount"]
    search_fields = ["sku", "product_name"]


class OrderHistoryViewSet(DatasetViewSet):
    """Every order status change ever recorded."""

    queryset = OrderHistory.objects.select_related("order").order_by("-created_at", "-id")
    serializer_class = OrderHistorySerializer
    filterset_class = OrderHistoryFilter
    ordering_fields = ["created_at", "order_id"]


class PaymentViewSet(DatasetViewSet):
    """Payment attempts including declines, retries, fees and settlement dates."""

    queryset = Payment.objects.select_related("order").order_by("-created_at", "-id")
    serializer_class = PaymentSerializer
    filterset_class = PaymentFilter
    ordering_fields = ["created_at", "amount", "fee_amount", "settled_at", "attempt"]
    search_fields = ["reference", "order__id", "failure_code"]


class RefundViewSet(DatasetViewSet):
    queryset = Refund.objects.select_related("order", "payment").order_by("-created_at", "-id")
    serializer_class = RefundSerializer
    filterset_class = RefundFilter
    ordering_fields = ["created_at", "processed_at", "amount"]
    search_fields = ["reference", "order__id"]


class ShipmentViewSet(DatasetViewSet):
    """Parcels with carrier, service level and the timestamps needed for delivery-time analysis."""

    queryset = Shipment.objects.select_related("order", "warehouse").order_by("-created_at", "-id")
    serializer_class = ShipmentSerializer
    filterset_class = ShipmentFilter
    ordering_fields = ["created_at", "shipped_at", "delivered_at", "cost", "weight_grams"]
    search_fields = ["tracking_number", "carrier", "order__id"]


class SubscriptionViewSet(DatasetViewSet):
    queryset = Subscription.objects.select_related("customer", "variant__product").order_by("-started_at", "-id")
    serializer_class = SubscriptionSerializer
    filterset_class = SubscriptionFilter
    ordering_fields = ["started_at", "cancelled_at", "unit_price"]
    search_fields = ["customer__username", "variant__sku"]

    @decorators.action(detail=True, methods=["get"])
    def events(self, request, pk=None):
        """The lifecycle of one subscription: renewals, skips, pauses, cancellation."""
        from .serializers import SubscriptionEventSerializer
        return response.Response(SubscriptionEventSerializer(self.get_object().events.all(), many=True).data)


class DiscountViewSet(viewsets.ReadOnlyModelViewSet):
    """Promo codes. Readable by any signed-in user; edited in the admin."""

    queryset = Discount.objects.select_related("campaign").order_by("code")
    serializer_class = DiscountSerializer
    permission_classes = [permissions.IsAuthenticated]
    pagination_class = DatasetPagination
    filterset_fields = ["kind", "active", "channel", "first_order_only", "campaign", "currency"]
    ordering_fields = ["code", "starts_at", "ends_at", "times_used", "value"]
    search_fields = ["code", "name", "description"]


class DiscountRedemptionViewSet(DatasetViewSet):
    """Which order used which code — the join that ``Discount.times_used`` cannot give you."""

    queryset = DiscountRedemption.objects.select_related("discount", "order", "customer").order_by("-created_at", "-id")
    serializer_class = DiscountRedemptionSerializer
    filterset_class = RedemptionFilter
    ordering_fields = ["created_at", "amount"]
    search_fields = ["code", "order__id", "customer__username"]
