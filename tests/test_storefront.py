import pytest
from django.urls import reverse

pytestmark = pytest.mark.django_db


def test_home_lists_live_categories_and_products(client, catalog):
    response = client.get(reverse("storefront"))
    assert response.status_code == 200
    html = response.content.decode()
    assert "Wireless Earbuds" in html and "Electronics" in html
    assert "Secret Draft" not in html and "Retired Kettle" not in html


def test_catalog_search_filter_sort_and_pagination(client, catalog, settings):
    settings.STOREFRONT_PAGE_SIZE = 2
    page1 = client.get(reverse("product_list"), {"sort": "price_asc"})
    names = [p.name for p in page1.context["page"].object_list]
    assert names == ["Table Lamp", "Wireless Earbuds"]
    page2 = client.get(reverse("product_list"), {"sort": "price_asc", "page": 2})
    assert [p.name for p in page2.context["page"].object_list] == ["Bluetooth Speaker"]
    search = client.get(reverse("product_list"), {"q": "lamp"})
    assert [p.name for p in search.context["page"].object_list] == ["Table Lamp"]
    in_stock = client.get(reverse("product_list"), {"in_stock": "1"})
    assert "Bluetooth Speaker" not in [p.name for p in in_stock.context["page"].object_list]
    priced = client.get(reverse("product_list"), {"min_price": "50", "max_price": "60"})
    assert [p.name for p in priced.context["page"].object_list] == ["Wireless Earbuds"]
    bad = client.get(reverse("product_list"), {"min_price": "abc"})
    assert bad.status_code == 200 and bad.context["errors"]


def test_category_page_and_empty_state(client, catalog):
    response = client.get(reverse("category_detail", args=["home"]))
    assert [p.name for p in response.context["page"].object_list] == ["Table Lamp"]
    empty = client.get(reverse("product_list"), {"q": "does-not-exist"})
    assert "No coffee matches that" in empty.content.decode()


def test_product_detail_visibility_rules(client, catalog, staff):
    assert client.get(reverse("product_detail", args=["p1"])).status_code == 200
    assert client.get(reverse("product_detail", args=["p4"])).status_code == 404  # draft
    assert client.get(reverse("product_detail", args=["p5"])).status_code == 404  # inactive
    assert client.get(reverse("product_detail", args=["nope"])).status_code == 404
    client.force_login(staff)
    preview = client.get(reverse("product_detail", args=["p4"]))
    assert preview.status_code == 200 and "Staff preview" in preview.content.decode()


def test_out_of_stock_product_has_no_add_to_cart(client, catalog):
    html = client.get(reverse("product_detail", args=["p2"])).content.decode()
    assert "Sold out" in html and 'action="/cart/add/"' not in html


def test_public_api_hides_non_live_products_but_staff_sees_all(client, catalog, staff):
    skus = {p["sku"] for p in client.get("/api/catalog/products/").json()["results"]}
    assert skus == {"P1", "P2", "P3"}
    client.force_login(staff)
    skus = {p["sku"] for p in client.get("/api/catalog/products/").json()["results"]}
    assert skus == {"P1", "P2", "P3", "P4", "P5"}
    detail = client.get(f"/api/catalog/products/{catalog['products']['P1'].id}/").json()
    assert detail["primary_image_url"].endswith("P1.jpg") and detail["in_stock"] is True

def test_category_counts_include_parent_and_children(client, catalog):
    from catalog.models import Category

    parent = Category.objects.create(name="Filter Coffee", slug="filter-coffee")
    africa = Category.objects.create(name="Africa", slug="africa", parent=parent)
    latin_america = Category.objects.create(
        name="Latin America",
        slug="latin-america",
        parent=parent,
    )

    catalog["products"]["P1"].category = africa
    catalog["products"]["P1"].save(update_fields=["category"])

    catalog["products"]["P2"].category = africa
    catalog["products"]["P2"].save(update_fields=["category"])

    catalog["products"]["P3"].category = latin_america
    catalog["products"]["P3"].save(update_fields=["category"])

    response = client.get(parent.get_absolute_url())

    assert response.status_code == 200

    parent_category = next(
        category
        for category in response.context["nav_categories"]
        if category.pk == parent.pk
    )
    assert parent_category.product_count == 3

    child_counts = {
        child.name: child.product_count
        for child in response.context["child_categories"]
    }
    assert child_counts == {
        "Africa": 2,
        "Latin America": 1,
    }


def test_child_category_breadcrumbs(client, catalog):
    from catalog.models import Category

    parent = Category.objects.create(name="Filter Coffee", slug="filter-coffee")
    child = Category.objects.create(
        name="Africa",
        slug="africa",
        parent=parent,
    )

    response = client.get(child.get_absolute_url())

    assert response.status_code == 200

    html = response.content.decode()
    assert "Home" in html
    assert "Shop" in html
    assert "Filter Coffee" in html
    assert "Africa" in html
    assert parent.get_absolute_url() in html


def test_subcategories_are_displayed_in_navigation(client, catalog):
    from catalog.models import Category

    parent = Category.objects.create(name="Filter Coffee", slug="filter-coffee")
    africa = Category.objects.create(name="Africa", slug="africa", parent=parent)
    latin_america = Category.objects.create(
        name="Latin America",
        slug="latin-america",
        parent=parent,
    )

    catalog["products"]["P1"].category = africa
    catalog["products"]["P1"].save(update_fields=["category"])

    catalog["products"]["P3"].category = latin_america
    catalog["products"]["P3"].save(update_fields=["category"])

    response = client.get(reverse("storefront"))

    assert response.status_code == 200

    html = response.content.decode()
    assert "Filter Coffee" in html
    assert "Africa" in html
    assert "Latin America" in html
    assert africa.get_absolute_url() in html
    assert latin_america.get_absolute_url() in html
