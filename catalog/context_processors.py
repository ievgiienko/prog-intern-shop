import os
from pathlib import Path

from django.conf import settings
from django.db.models import Count, Prefetch, Q

from .models import Category, live_category_counts


def _asset_version():
    """mtime of the storefront CSS/JS, appended as ?v= so browsers never serve a stale stylesheet."""
    base = Path(settings.BASE_DIR) / "static"
    try:
        return str(int(max(os.path.getmtime(base / p) for p in ("css/storefront.css", "js/storefront.js"))))
    except OSError:
        return "1"


ASSET_VERSION = _asset_version()


def storefront(request):
    """Navigation data every storefront page needs: live categories and the cart badge."""
    categories = live_category_counts().prefetch_related(
        Prefetch(
            "children",
            queryset=Category.objects
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
                .order_by("position", "name"),
        )
    )
    cart_count = 0
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated:
        from sales.models import CartItem
        cart_count = CartItem.objects.filter(cart__customer=user, cart__active=True).aggregate(
            n=Count("id"))["n"] or 0
    elif hasattr(request, "session") and request.session.get("cart_id"):
        from sales.models import CartItem
        cart_count = CartItem.objects.filter(cart_id=request.session["cart_id"], cart__active=True).aggregate(
            n=Count("id"))["n"] or 0
    return {"nav_categories": categories, "cart_count": cart_count,
            "STORE_NAME": settings.STORE_NAME, "STORE_SHORT_NAME": settings.STORE_SHORT_NAME,
            "STORE_TAGLINE": settings.STORE_TAGLINE, "CURRENCY": settings.STORE_CURRENCY, "SITE_URL": settings.SITE_URL, "ASSET_VERSION": ASSET_VERSION}
