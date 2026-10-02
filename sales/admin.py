from django.contrib import admin, messages
from django.db.models import Count
from rest_framework.exceptions import ValidationError

from .models import (Cart, CartItem, Discount, DiscountRedemption, Order, OrderHistory, OrderItem,
                     Payment, Refund, ReturnRequest, Shipment, Subscription, SubscriptionEvent)
from .services import transition


class OrderItemInline(admin.TabularInline):
    model = OrderItem
    extra = 0
    can_delete = False
    fields = ("sku", "product_name", "quantity", "unit_price", "unit_cost", "tax_rate", "line_total", "warehouse")
    readonly_fields = fields

    @admin.display(description="Line total")
    def line_total(self, obj):
        return obj.unit_price * obj.quantity

    def has_add_permission(self, request, obj=None):
        return False


class OrderHistoryInline(admin.TabularInline):
    model = OrderHistory
    extra = 0
    can_delete = False
    fields = ("created_at", "from_status", "to_status", "note")
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


class PaymentInline(admin.TabularInline):
    model = Payment
    extra = 0
    can_delete = False
    fields = ("created_at", "reference", "method", "gateway", "card_last4", "amount", "fee_amount",
              "refunded_amount", "status", "attempt", "failure_code", "message")
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


STAFF_CANCEL_REASONS = [
    Order.CancelReason.CUSTOMER_REQUEST,
    Order.CancelReason.SUSPECTED_FRAUD,
    Order.CancelReason.DUPLICATE,
    Order.CancelReason.OUT_OF_STOCK,
    Order.CancelReason.ADDRESS_INVALID,
]  # PAYMENT_TIMEOUT / PAYMENT_FAILED are set by the system, staff do not pick them


def _cancel_action(reason):
    @admin.action(description=f"Cancel: {Order.CancelReason(reason).label} (releases reserved stock)")
    def action(modeladmin, request, queryset):
        modeladmin._transition(request, queryset, "CANCELLED", "Cancelled", cancel_reason=reason)

    action.__name__ = f"cancel_{reason.lower()}"
    return action


CANCEL_ACTIONS = [_cancel_action(reason) for reason in STAFF_CANCEL_REASONS]


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ("id", "customer", "status", "total", "currency", "items_count", "warehouse", "channel_group",
                    "market", "device", "fraud_score", "created_at")
    list_filter = ("status", "warehouse", "channel_group", "currency", "market", "device", "is_test_data",
                   "source", "created_at")
    search_fields = ("id", "customer__username", "customer__email", "shipping_name", "contact_email")
    date_hierarchy = "created_at"
    list_select_related = ("customer", "warehouse")
    readonly_fields = ("customer", "warehouse", "subtotal", "discount_amount", "tax_amount", "total", "fraud_score",
                       "source", "status", "access_token", "created_at", "updated_at", "channel", "channel_group",
                       "utm_source", "utm_medium", "utm_campaign", "utm_content", "referrer_domain", "landing_page",
                       "device", "browser", "os", "session_id", "campaign", "coupon_code", "paid_at", "shipped_at",
                       "delivered_at", "cancelled_at", "refunded_at")
    inlines = [OrderItemInline, PaymentInline, OrderHistoryInline]
    actions = ["mark_processing", "mark_packed", "mark_shipped", "mark_delivered", *CANCEL_ACTIONS]
    fieldsets = (
        (None, {"fields": ("customer", "status", "warehouse", "source", "fraud_score", "access_token", "is_test_data")}),
        ("Amounts", {"fields": ("subtotal", "discount_amount", "shipping_amount", "tax_amount", "total",
                                "currency", "fx_rate")}),
        ("Shipping & contact", {"fields": ("shipping_name", "shipping_address", "shipping_city", "shipping_postal_code",
                                           "shipping_country", "contact_email", "contact_phone", "customer_note")}),
        ("Attribution", {"classes": ("collapse",),
                         "fields": ("channel", "channel_group", "utm_source", "utm_medium", "utm_campaign",
                                    "utm_content", "referrer_domain", "landing_page", "device", "browser", "os",
                                    "session_id", "campaign", "coupon_code")}),
        ("Timestamps", {"classes": ("collapse",),
                        "fields": ("created_at", "paid_at", "shipped_at", "delivered_at", "cancelled_at",
                                   "refunded_at", "updated_at")}),
    )

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(n_items=Count("items"))

    def has_add_permission(self, request):
        return False   # orders are created through checkout so that stock is reserved correctly

    @admin.display(description="Items", ordering="n_items")
    def items_count(self, obj):
        return obj.n_items

    def _transition(self, request, queryset, status, label, cancel_reason=""):
        done, failed = 0, []
        for order in queryset:
            try:
                transition(order, status, note=f"{label} via admin by {request.user.username}",
                           cancel_reason=cancel_reason)
                done += 1
            except ValidationError as exc:
                failed.append(f"#{order.id}: {exc.detail if isinstance(exc.detail, str) else ' '.join(map(str, exc.detail))}")
        if done:
            self.message_user(request, f"{done} order(s) moved to {status}.", messages.SUCCESS)
        for line in failed:
            self.message_user(request, line, messages.WARNING)

    @admin.action(description="Mark as PROCESSING (paid orders)")
    def mark_processing(self, request, queryset):
        self._transition(request, queryset, "PROCESSING", "Processing")

    @admin.action(description="Mark as PACKED")
    def mark_packed(self, request, queryset):
        self._transition(request, queryset, "PACKED", "Packed")

    @admin.action(description="Mark as SHIPPED (deducts stock)")
    def mark_shipped(self, request, queryset):
        self._transition(request, queryset, "SHIPPED", "Shipped")

    @admin.action(description="Mark as DELIVERED")
    def mark_delivered(self, request, queryset):
        self._transition(request, queryset, "DELIVERED", "Delivered")


class CartItemInline(admin.TabularInline):
    model = CartItem
    extra = 0
    autocomplete_fields = ("variant",)


@admin.register(Cart)
class CartAdmin(admin.ModelAdmin):
    list_display = ("id", "customer", "is_guest", "active", "items_count", "discount", "updated_at")
    list_filter = ("active", ("customer", admin.EmptyFieldListFilter))
    search_fields = ("customer__username", "customer__email")
    raw_id_fields = ("customer",)
    inlines = [CartItemInline]

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(n_items=Count("items"))

    @admin.display(description="Items", ordering="n_items")
    def items_count(self, obj):
        return obj.n_items

    @admin.display(description="Guest", boolean=True)
    def is_guest(self, obj):
        return obj.customer_id is None


@admin.register(Discount)
class DiscountAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "kind", "value", "minimum_cart", "channel", "campaign", "starts_at",
                    "ends_at", "usage", "active")
    list_filter = ("kind", "active", "channel", "first_order_only")
    search_fields = ("code",)

    @admin.display(description="Used / limit")
    def usage(self, obj):
        return f"{obj.times_used} / {obj.usage_limit}"


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ("reference", "order", "method", "amount", "currency", "fee_amount", "refunded_amount",
                    "status", "attempt", "failure_code", "created_at")
    list_filter = ("status", "method", "currency", "gateway", "failure_code")
    search_fields = ("reference", "order__id")
    readonly_fields = ("order", "amount", "reference", "created_at")


@admin.register(ReturnRequest)
class ReturnRequestAdmin(admin.ModelAdmin):
    list_display = ("id", "order_item", "quantity", "reason", "status", "refund_amount", "restock", "created_at")
    list_filter = ("status", "reason", "restock", "damaged")
    search_fields = ("order_item__order__id", "order_item__sku")
    raw_id_fields = ("order_item",)


@admin.register(OrderItem)
class OrderItemAdmin(admin.ModelAdmin):
    list_display = ("order", "sku", "product_name", "quantity", "unit_price")
    search_fields = ("sku", "product_name", "order__id")
    readonly_fields = [f.name for f in OrderItem._meta.fields]


@admin.register(OrderHistory)
class OrderHistoryAdmin(admin.ModelAdmin):
    list_display = ("order", "from_status", "to_status", "note", "created_at")
    list_filter = ("to_status",)
    search_fields = ("order__id",)


@admin.register(CartItem)
class CartItemAdmin(admin.ModelAdmin):
    list_display = ("cart", "variant", "quantity")
    autocomplete_fields = ("variant",)


@admin.register(Shipment)
class ShipmentAdmin(admin.ModelAdmin):
    list_display = ("tracking_number", "order", "carrier", "service_level", "status", "destination_country_code",
                    "shipped_at", "delivered_at", "cost")
    list_filter = ("status", "carrier", "service_level", "destination_country_code")
    search_fields = ("tracking_number", "order__id")
    date_hierarchy = "created_at"
    raw_id_fields = ("order",)


@admin.register(Refund)
class RefundAdmin(admin.ModelAdmin):
    list_display = ("id", "order", "amount", "currency", "reason", "status", "initiated_by", "created_at")
    list_filter = ("status", "reason", "initiated_by", "currency")
    search_fields = ("reference", "order__id")
    date_hierarchy = "created_at"
    raw_id_fields = ("order", "payment", "return_request")


class SubscriptionEventInline(admin.TabularInline):
    model = SubscriptionEvent
    extra = 0
    can_delete = False
    fields = ("created_at", "kind", "from_status", "to_status", "note")
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = ("id", "customer", "variant", "plan", "status", "quantity", "unit_price", "currency",
                    "started_at", "cancelled_at")
    list_filter = ("status", "plan", "currency", "is_test_data")
    search_fields = ("customer__username", "variant__sku")
    raw_id_fields = ("customer", "variant")
    date_hierarchy = "started_at"
    inlines = [SubscriptionEventInline]


@admin.register(DiscountRedemption)
class DiscountRedemptionAdmin(admin.ModelAdmin):
    list_display = ("code", "order", "customer", "amount", "currency", "created_at")
    list_filter = ("code", "currency")
    search_fields = ("code", "order__id", "customer__username")
    raw_id_fields = ("discount", "order", "customer")
