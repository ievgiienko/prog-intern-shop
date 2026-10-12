import os
from pathlib import Path

from django.conf import settings
from django.db.models import Count, Q, Sum

from .models import Category, live_category_counts

from sales import services


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
    categories = live_category_counts()
    cart_count = 0
    cart = None
    user = getattr(request, "user", None)
    if (user is not None and user.is_authenticated) or (hasattr(request, "session") and request.session.get("cart_id")): cart = services.get_cart_for_request(request, create=False)
    if cart:
        cart_count = cart.item_count
    return {"nav_categories": categories, "cart_count": cart_count, "cart": cart,
            "STORE_NAME": settings.STORE_NAME, "STORE_SHORT_NAME": settings.STORE_SHORT_NAME,
            "STORE_TAGLINE": settings.STORE_TAGLINE, "CURRENCY": settings.STORE_CURRENCY, "SITE_URL": settings.SITE_URL, "ASSET_VERSION": ASSET_VERSION}
