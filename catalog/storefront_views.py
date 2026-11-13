import json

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Avg, Case, Count, F, IntegerField, Q, Value, When
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from accounts.models import NewsletterSubscriber

from .content import BREW_METHODS, COFFEE_FAQ, EQUIPMENT_FAQ, HOME_FAQ, SHIPPING_FAQ, STORE_FACTS, TRUST_POINTS
from .forms import NewsletterForm, ReviewForm
from .models import live_category_counts, Category, Product, Review

SORT_OPTIONS = {
    "featured": ("Featured", ["-featured", "-created_at", "-id"]),
    "newest": ("Newest", ["-created_at", "-id"]),
    "price_asc": ("Price: low to high", ["sale_price", "id"]),
    "price_desc": ("Price: high to low", ["-sale_price", "id"]),
    "name": ("Name A–Z", ["name", "id"]),
}
PAID_STATUSES = {"PAID", "PROCESSING", "PACKED", "SHIPPED", "DELIVERED"}


def _visible_products():
    return (Product.objects.storefront().with_stock()
            .select_related("brand", "category").prefetch_related("images", "variants"))


def _live_categories():
    return live_category_counts()


def _safe_next(request, fallback):
    target = request.POST.get("next") or request.GET.get("next")
    if target and url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()}):
        return target
    return fallback


# ---------------------------------------------------------------------------
# Home
# ---------------------------------------------------------------------------

def storefront(request):
    """Landing page. URL name kept as `storefront` for compatibility."""
    products = _visible_products()
    kind_rank = Case(When(kind=Product.Kind.COFFEE, then=Value(0)), When(kind=Product.Kind.BUNDLE, then=Value(1)),
                     When(kind=Product.Kind.SUBSCRIPTION, then=Value(2)), default=Value(3), output_field=IntegerField())
    featured = list(products.filter(featured=True).annotate(kind_rank=kind_rank).order_by("kind_rank", "-created_at", "-id")[:8])
    if len(featured) < 4:  # a freshly configured store still gets a full row
        extra = products.exclude(pk__in=[p.pk for p in featured]).order_by("-created_at", "-id")[:4 - len(featured)]
        featured += list(extra)
    hero_product = next((p for p in featured if p.is_coffee), featured[0] if featured else None)
    newest = products.order_by("-created_at", "-id")[:4]
    on_sale = products.filter(compare_at_price__gt=F("sale_price")).order_by("-created_at")[:3]
    subscription = products.filter(kind=Product.Kind.SUBSCRIPTION).order_by("sale_price").first()
    reviews = (Review.objects.filter(status=Review.Status.APPROVED, rating__gte=4, product__active=True,
                                     product__status=Product.Status.ACTIVE)
               .exclude(comment="").select_related("customer", "product").order_by("-created_at")[:3])
    rating = Review.objects.filter(status=Review.Status.APPROVED).aggregate(avg=Avg("rating"), n=Count("id"))
    return render(request, "catalog/home.html", {
        "categories": _live_categories(), "featured": featured, "hero_product": hero_product, "newest": newest, "on_sale": on_sale,
        "subscription": subscription, "reviews": reviews, "rating": rating, "faq": HOME_FAQ, "trust": TRUST_POINTS,
        "brew_methods": [(code, *BREW_METHODS[code]) for code in ("v60", "espresso", "aeropress", "french_press", "moka", "cold_brew")],
        "roasts": Product.Roast.choices, "facts": STORE_FACTS, "newsletter_form": NewsletterForm(initial={"source": "home"}),
        "og_image": "https://images.unsplash.com/photo-1500557515707-69f05f65df7d",
        "meta_description": f"{settings.STORE_NAME}: small-batch specialty coffee roasted to order in Valencia. "
                            "Single origins, espresso blends, decaf, subscriptions and brewing gear with free EU shipping.",
    })


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------

def product_list(request, category_slug=None):
    category = get_object_or_404(Category, slug=category_slug) if category_slug else None
    params = request.GET
    products = _visible_products()
    if category:
        products = products.filter(Q(category=category) | Q(category__parent=category))
    query = params.get("q", "").strip()
    if query:
        products = products.filter(Q(name__icontains=query) | Q(short_description__icontains=query)
                                   | Q(description__icontains=query) | Q(tasting_notes__icontains=query)
                                   | Q(origin__icontains=query) | Q(region__icontains=query)
                                   | Q(sku__icontains=query) | Q(brand__name__icontains=query))
    brand_slug = params.get("brand", "")
    if brand_slug:
        products = products.filter(brand__slug=brand_slug)
    selected = {
        "origin": params.getlist("origin"),
        "roast": [r for r in params.getlist("roast") if r in Product.Roast.values],
        "brew": [b for b in params.getlist("brew") if b in BREW_METHODS],
    }
    if selected["origin"]:
        origin_q = Q()
        for origin in selected["origin"]:
            origin_q |= Q(origin__icontains=origin)
        products = products.filter(origin_q)
    if selected["roast"]:
        products = products.filter(roast_level__in=selected["roast"])
    if selected["brew"]:
        brew_q = Q()
        for code in selected["brew"]:
            brew_q |= Q(brew_methods__icontains=code)
        products = products.filter(brew_q)
    errors = []
    for key, lookup in (("min_price", "sale_price__gte"), ("max_price", "sale_price__lte")):
        value = params.get(key, "").strip()
        if value:
            try:
                products = products.filter(**{lookup: float(value)})
            except ValueError:
                errors.append(f"{key.replace('_', ' ').capitalize()} must be a number.")
    if params.get("in_stock"):
        products = products.filter(stock_available__gt=0)
    if params.get("on_sale"):
        products = products.filter(compare_at_price__gt=F("sale_price"))
    sort = params.get("sort") if params.get("sort") in SORT_OPTIONS else "featured"
    products = products.order_by(*SORT_OPTIONS[sort][1])

    paginator = Paginator(products, settings.STOREFRONT_PAGE_SIZE)
    page = paginator.get_page(params.get("page"))
    querystring = params.copy()
    querystring.pop("page", None)

    # Facet values come from the live catalog so the filters never offer empty choices.
    scope = _visible_products()
    if category:
        scope = scope.filter(Q(category=category) | Q(category__parent=category))
    origins = sorted({o.strip() for row in scope.exclude(origin="").values_list("origin", flat=True)
                      for o in row.split(",") if o.strip()})
    roasts = [(code, label) for code, label in Product.Roast.choices
              if scope.filter(roast_level=code).exists()]
    brew_codes = {c for row in scope.exclude(brew_methods="").values_list("brew_methods", flat=True)
                  for c in Product._split(row)}
    brews = [(code, BREW_METHODS[code][0]) for code in BREW_METHODS if code in brew_codes]
    active_filters = bool(query or selected["origin"] or selected["roast"] or selected["brew"] or params.get("min_price")
                          or params.get("max_price") or params.get("in_stock") or params.get("on_sale") or brand_slug)
    if category:
        title, description = category.name, category.description
    elif query:
        title, description = f"Results for “{query}”", ""
    else:
        title, description = "All coffee & equipment", "Everything we roast and every tool we trust, in one place."

    child_categories = []
    if category:
        child_categories = (
            category.children
            .annotate(
                product_count=Count(
                    "products",
                    filter=Q(
                        products__active=True,
                        products__status="ACTIVE",
                    ),
                    distinct=True,
                )
            )
            .filter(product_count__gt=0)
            .order_by("position", "name")
        )
    return render(request, "catalog/product_list.html", {
        "category": category, "page": page, "paginator": paginator, "query": query, "sort": sort,
        "sort_options": SORT_OPTIONS, "selected": selected, "origins": origins, "roasts": roasts, "brews": brews,
        "querystring": querystring.urlencode(), "errors": errors, "filters": params, "total": paginator.count,
        "active_filters": active_filters, "title": title, "description": description,
        "meta_description": (description or f"Shop {title.lower()} at {settings.STORE_NAME}.")[:160],
        "categories": _live_categories(),
        "child_categories": child_categories,
    })


def product_detail(request, slug):
    product = get_object_or_404(
        Product.objects.select_related("brand", "category").prefetch_related("images", "variants__inventory"), slug=slug)
    if not product.is_visible and not request.user.is_staff:
        raise Http404("Product not available")
    variants = [v for v in product.variants.all() if v.active]
    reviews = product.reviews.filter(status=Review.Status.APPROVED).select_related("customer")
    rating = reviews.aggregate(avg=Avg("rating"), n=Count("id"))
    related = list(_visible_products().filter(category=product.category).exclude(pk=product.pk).order_by("-featured", "-created_at")[:4])
    if len(related) < 4:
        related += list(_visible_products().filter(featured=True).exclude(pk__in=[product.pk] + [r.pk for r in related])
                        .order_by("-created_at")[:4 - len(related)])

    user = request.user
    own_review = None
    verified = False
    if user.is_authenticated:
        own_review = product.reviews.filter(customer=user).first()
        from sales.models import OrderItem
        verified = OrderItem.objects.filter(order__customer=user, variant__product=product,
                                            order__status__in=PAID_STATUSES).exists()
    images = list(product.images.all())
    faq = (EQUIPMENT_FAQ + SHIPPING_FAQ[:2]) if product.kind == Product.Kind.EQUIPMENT else (COFFEE_FAQ + SHIPPING_FAQ[:1])
    return render(request, "catalog/product_detail.html", {
        "product": product, "variants": variants, "images": images, "reviews": reviews[:10], "rating": rating,
        "related": related, "preview_only": not product.is_visible, "faq": faq, "trust": TRUST_POINTS[:3],
        "review_form": ReviewForm(), "own_review": own_review, "verified_purchase": verified,
        "brew_methods": [(code, *BREW_METHODS[code]) for code in product.brew_method_codes if code in BREW_METHODS],
        "json_ld": _product_json_ld(request, product, variants, images, rating),
        "meta_description": (product.short_description or product.description)[:160],
        "og_image": images[0].src if images else "",
    })


def _product_json_ld(request, product, variants, images, rating):
    prices = [float(v.effective_price) for v in variants] or [float(product.sale_price)]
    data = {
        "@context": "https://schema.org", "@type": "Product", "name": product.name, "sku": product.sku,
        "description": product.short_description or product.description[:300],
        "brand": {"@type": "Brand", "name": product.brand.name},
        "image": [request.build_absolute_uri(i.src) if i.src.startswith("/") else i.src for i in images],
        "url": request.build_absolute_uri(product.get_absolute_url()),
        "offers": {
            "@type": "AggregateOffer" if len(set(prices)) > 1 else "Offer", "priceCurrency": "EUR",
            "availability": "https://schema.org/InStock" if product.in_stock else "https://schema.org/OutOfStock",
            "url": request.build_absolute_uri(product.get_absolute_url()),
        },
    }
    if len(set(prices)) > 1:
        data["offers"].update({"lowPrice": f"{min(prices):.2f}", "highPrice": f"{max(prices):.2f}", "offerCount": len(prices)})
    else:
        data["offers"]["price"] = f"{prices[0]:.2f}"
    if rating["n"]:
        data["aggregateRating"] = {"@type": "AggregateRating", "ratingValue": f"{rating['avg']:.1f}", "reviewCount": rating["n"]}
    return json.dumps(data)


@login_required
@require_POST
def review_create(request, slug):
    product = get_object_or_404(Product.objects.storefront(), slug=slug)
    if product.reviews.filter(customer=request.user).exists():
        messages.info(request, "You have already reviewed this coffee — thank you!")
        return redirect(product.get_absolute_url())
    form = ReviewForm(request.POST)
    if form.is_valid():
        from sales.models import OrderItem
        review = form.save(commit=False)
        review.customer, review.product = request.user, product
        review.verified_purchase = OrderItem.objects.filter(order__customer=request.user, variant__product=product,
                                                            order__status__in=PAID_STATUSES).exists()
        review.save()
        messages.success(request, "Thanks for your review! It will appear once our team has checked it.")
    else:
        messages.error(request, "Please choose a rating between 1 and 5 stars.")
    return redirect(product.get_absolute_url() + "#reviews")


# ---------------------------------------------------------------------------
# Newsletter & static pages
# ---------------------------------------------------------------------------

@require_POST
def newsletter_subscribe(request):
    form = NewsletterForm(request.POST)
    back = _safe_next(request, reverse("storefront"))
    if not form.is_valid():
        messages.error(request, "Please enter a valid e-mail address.")
        return redirect(back)
    email = form.cleaned_data["email"].lower()
    _, created = NewsletterSubscriber.objects.get_or_create(
        email=email, defaults={"source": form.cleaned_data.get("source") or "footer"})
    messages.success(request, "You're on the list — fresh-crop news and 10% off your next bag are on their way."
                     if created else "You're already subscribed — thank you!")
    return redirect(back)


def about(request):
    return render(request, "catalog/about.html", {
        "facts": STORE_FACTS, "trust": TRUST_POINTS,
        "meta_description": f"The story of {settings.STORE_NAME}: a small roastery in Valencia sourcing traceable coffee from nine origins."})


def shipping_returns(request):
    return render(request, "catalog/shipping.html", {
        "faq": SHIPPING_FAQ + EQUIPMENT_FAQ[1:2], "meta_description": "Shipping times, costs, returns and the happiness guarantee."})


def robots_txt(request):
    lines = ["User-agent: *", "Disallow: /admin/", "Disallow: /cart/", "Disallow: /checkout/", "Disallow: /orders/",
             "Disallow: /accounts/", f"Sitemap: {request.build_absolute_uri('/sitemap.xml')}"]
    return HttpResponse("\n".join(lines), content_type="text/plain")
