from django import forms
from django.contrib import admin, messages
from django.db.models import Count, Q
from django.utils.html import format_html

from .content import BREW_METHODS
from .models import (Brand, Category, PriceHistory, Product, ProductImage, ProductStatusHistory,
                     ProductVariant, Review, Tag, Wishlist)


class ProductAdminForm(forms.ModelForm):
    """Brew methods are stored as a CSV of codes; staff pick them from checkboxes."""
    brew_methods = forms.MultipleChoiceField(
        choices=[(code, label) for code, (label, _, _) in BREW_METHODS.items()], required=False,
        widget=forms.CheckboxSelectMultiple, help_text="Shown as 'Suitable for' icons and used by the catalog filter.")

    class Meta:
        model = Product
        fields = "__all__"
        widgets = {"short_description": forms.TextInput(attrs={"size": 100}),
                   "tasting_notes": forms.TextInput(attrs={"size": 60}),
                   "highlights": forms.Textarea(attrs={"rows": 4}), "brew_guide": forms.Textarea(attrs={"rows": 6}),
                   "origin_story": forms.Textarea(attrs={"rows": 6})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            self.initial["brew_methods"] = self.instance.brew_method_codes

    def clean_brew_methods(self):
        return ",".join(self.cleaned_data["brew_methods"])


class ProductVariantInline(admin.TabularInline):
    model = ProductVariant
    extra = 0
    fields = ("sku", "name", "attributes", "price", "active", "stock")
    readonly_fields = ("stock",)

    @admin.display(description="Available stock")
    def stock(self, obj):
        return obj.available_quantity if obj.pk else "—"


class ProductImageInline(admin.TabularInline):
    model = ProductImage
    extra = 0
    fields = ("preview", "image", "url", "alt_text", "primary", "position")
    readonly_fields = ("preview",)

    @admin.display(description="Preview")
    def preview(self, obj):
        if obj.pk and obj.src:
            return format_html('<img src="{}" alt="" style="height:48px;width:64px;object-fit:cover;border-radius:4px">', obj.src)
        return "—"


class PriceHistoryInline(admin.TabularInline):
    model = PriceHistory
    extra = 0
    can_delete = False
    fields = ("changed_at", "old_price", "new_price")
    readonly_fields = fields
    ordering = ("-changed_at",)

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    form = ProductAdminForm
    list_display = ("thumbnail", "sku", "name", "category", "origin", "roast_level", "sale_price", "compare_at_price",
                    "stock", "featured", "status", "active")
    list_display_links = ("thumbnail", "sku", "name")
    list_editable = ("sale_price", "compare_at_price", "featured", "status", "active")
    list_filter = ("active", "status", "featured", "kind", "category", "tags", "roast_level", "process",
                   "brand", "is_limited", "is_seasonal")
    filter_horizontal = ("tags",)
    search_fields = ("sku", "name", "short_description", "description", "origin", "region", "tasting_notes", "variants__sku")
    prepopulated_fields = {"slug": ("name",)}
    ordering = ("-created_at",)
    date_hierarchy = "created_at"
    list_per_page = 25
    list_select_related = ("category", "brand")
    autocomplete_fields = ("category", "brand")
    readonly_fields = ("created_at", "updated_at", "margin")
    inlines = [ProductVariantInline, ProductImageInline, PriceHistoryInline]
    actions = ["activate", "deactivate", "mark_discontinued", "mark_draft"]
    fieldsets = (
        (None, {"fields": ("name", "slug", "sku", "kind", "category", "brand", "short_description", "description")}),
        ("Visibility & merchandising", {"fields": ("status", "active", "featured", "tags", "is_limited",
                                                   "is_seasonal", "launched_at", "discontinued_at"),
                                        "description": "A product is visible in the storefront only when it is Active and the status is ACTIVE. "
                                                       "Featured products appear in the home page best-sellers row."}),
        ("Pricing", {"fields": ("sale_price", "compare_at_price", "purchase_cost", "tax_rate", "margin")}),
        ("Coffee profile", {"fields": ("origin", "origin_country_code", "region", "producer", "variety", "process",
                                       "roast_level", "altitude", "weight_grams", "tasting_notes", "brew_methods"),
                            "description": "Leave blank for equipment. Shown in the 'Coffee profile' block and used by the catalog filters."}),
        ("Storefront content", {"fields": ("highlights", "brew_guide", "origin_story", "story_image_url", "specs"),
                                "description": "Optional editorial blocks on the product page."}),
        ("Replenishment", {"classes": ("collapse",), "fields": ("reorder_point", "preferred_reorder_quantity", "safety_stock")}),
        ("Timestamps", {"classes": ("collapse",), "fields": ("created_at", "updated_at")}),
    )

    def get_queryset(self, request):
        return super().get_queryset(request).with_stock().prefetch_related("images")

    @admin.display(description="")
    def thumbnail(self, obj):
        image = obj.primary_image
        if image:
            return format_html('<img src="{}" alt="" style="height:40px;width:54px;object-fit:cover;border-radius:4px">', image.src)
        return format_html('<span style="display:inline-block;height:40px;width:54px;background:#e5e7eb;border-radius:4px"></span>')

    @admin.display(description="Stock", ordering="stock_available")
    def stock(self, obj):
        qty = obj.available_quantity
        colour = "#b91c1c" if qty <= 0 else "#b45309" if qty <= obj.reorder_point else "#166534"
        return format_html('<strong style="color:{}">{}</strong>', colour, qty)

    @admin.display(description="Margin")
    def margin(self, obj):
        return f"{obj.margin_percent}%"

    def save_formset(self, request, form, formset, change):
        super().save_formset(request, form, formset, change)
        if formset.model is ProductImage:
            images = list(form.instance.images.order_by("-primary", "position", "id"))
            primaries = [i for i in images if i.primary]
            if images and len(primaries) != 1:
                for image in images:
                    image.primary = image is (primaries[0] if primaries else images[0])
                ProductImage.objects.bulk_update(images, ["primary"])

    def _set(self, request, queryset, **fields):
        updated = 0
        for product in queryset:
            for name, value in fields.items():
                setattr(product, name, value)
            product.save()
            updated += 1
        self.message_user(request, f"{updated} product(s) updated.", messages.SUCCESS)

    @admin.action(description="Activate selected products (visible in the storefront)")
    def activate(self, request, queryset):
        self._set(request, queryset, active=True, status=Product.Status.ACTIVE)

    @admin.action(description="Deactivate selected products (hide from the storefront)")
    def deactivate(self, request, queryset):
        self._set(request, queryset, active=False)

    @admin.action(description="Mark selected products as discontinued")
    def mark_discontinued(self, request, queryset):
        self._set(request, queryset, status=Product.Status.DISCONTINUED)

    @admin.action(description="Move selected products back to draft")
    def mark_draft(self, request, queryset):
        self._set(request, queryset, status=Product.Status.DRAFT)


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ("thumbnail", "name", "slug", "position", "parent", "live_products", "total_products")
    list_display_links = ("thumbnail", "name")
    list_editable = ("position",)
    list_filter = ("parent",)
    search_fields = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}
    fields = ("name", "slug", "parent", "position", "description", "image", "image_url")

    @admin.display(description="")
    def thumbnail(self, obj):
        if obj.image_src:
            return format_html('<img src="{}" alt="" style="height:40px;width:54px;object-fit:cover;border-radius:4px">', obj.image_src)
        return "—"

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(
            live=Count("products", filter=Q(products__active=True, products__status="ACTIVE"), distinct=True),
            total=Count("products", distinct=True))

    @admin.display(description="Live products", ordering="live")
    def live_products(self, obj):
        return obj.live

    @admin.display(description="All products", ordering="total")
    def total_products(self, obj):
        return obj.total


@admin.register(Brand)
class BrandAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "product_count")
    search_fields = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(n=Count("products"))

    @admin.display(description="Products", ordering="n")
    def product_count(self, obj):
        return obj.n


@admin.register(ProductVariant)
class ProductVariantAdmin(admin.ModelAdmin):
    list_display = ("sku", "product", "name", "price", "effective_price", "stock", "active")
    list_filter = ("active", "product__category")
    search_fields = ("sku", "name", "product__name", "product__sku")
    autocomplete_fields = ("product",)
    list_select_related = ("product",)

    @admin.display(description="Available stock")
    def stock(self, obj):
        return obj.available_quantity


@admin.register(ProductImage)
class ProductImageAdmin(admin.ModelAdmin):
    @admin.display(description="Source")
    def source(self, obj):
        return "upload" if obj.image else "url"

    list_display = ("product", "source", "alt_text", "primary", "position")
    list_filter = ("primary",)
    search_fields = ("product__name", "product__sku", "alt_text")
    autocomplete_fields = ("product",)


@admin.register(PriceHistory)
class PriceHistoryAdmin(admin.ModelAdmin):
    list_display = ("changed_at", "product", "old_price", "new_price")
    list_select_related = ("product",)
    search_fields = ("product__name", "product__sku")
    date_hierarchy = "changed_at"
    readonly_fields = ("product", "old_price", "new_price", "changed_at")

    def has_add_permission(self, request):
        return False


@admin.register(Review)
class ReviewAdmin(admin.ModelAdmin):
    list_display = ("product", "customer", "rating", "status", "channel", "country_code", "helpful_votes",
                    "verified_purchase", "created_at")
    list_filter = ("status", "rating", "channel", "verified_purchase", "country_code")
    search_fields = ("product__name", "customer__username", "comment")
    list_select_related = ("product", "customer")
    actions = ["approve", "reject"]
    autocomplete_fields = ("product",)
    raw_id_fields = ("customer",)

    def _moderate(self, request, queryset, status, label):
        count = 0
        for review in queryset:
            review.status = status
            review.save()
            count += 1
        self.message_user(request, f"{count} review(s) {label}.")

    @admin.action(description="Approve selected reviews")
    def approve(self, request, queryset):
        self._moderate(request, queryset, Review.Status.APPROVED, "approved")

    @admin.action(description="Reject selected reviews")
    def reject(self, request, queryset):
        self._moderate(request, queryset, Review.Status.REJECTED, "rejected")


@admin.register(Wishlist)
class WishlistAdmin(admin.ModelAdmin):
    list_display = ("customer", "product_count")
    raw_id_fields = ("customer",)
    filter_horizontal = ("products",)

    @admin.display(description="Products")
    def product_count(self, obj):
        return obj.products.count()


@admin.register(Tag)
class TagAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "group", "product_count", "description")
    list_filter = ("group",)
    search_fields = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(n_products=Count("products"))

    @admin.display(description="Products", ordering="n_products")
    def product_count(self, obj):
        return obj.n_products


@admin.register(ProductStatusHistory)
class ProductStatusHistoryAdmin(admin.ModelAdmin):
    list_display = ("changed_at", "product", "from_status", "to_status", "note")
    list_filter = ("to_status",)
    search_fields = ("product__name", "product__sku")
    date_hierarchy = "changed_at"
    raw_id_fields = ("product",)
