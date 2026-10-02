"""Seed the Sondermark Coffee Roasters demo store: roles, staff accounts, warehouses, suppliers,
the coffee catalog (catalog/demo_catalog.py), reviews, campaigns and a welcome discount.

Idempotent: re-running updates editorial content but never duplicates products, users or stock rows.
"""
import random
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.core.management.base import BaseCommand
from django.utils import timezone
from django.utils.text import slugify

from accounts.models import CustomerProfile, NewsletterSubscriber
from catalog.demo_catalog import (BRANDS, CAMPAIGNS, CATEGORIES, COFFEE_VARIANTS, DISCOUNTS, PRODUCTS, REVIEWERS,
                                  REVIEWS, SUBCATEGORIES, TAGS, unsplash)
from catalog.models import (Brand, Category, PriceHistory, Product, ProductImage, ProductStatusHistory,
                            ProductVariant, Review, Tag)
from operations.models import (Campaign, Inventory, InventorySnapshot, PurchaseOrder, PurchaseOrderItem, Supplier,
                               SupplierProduct, Transfer, Warehouse)
from sales.models import Discount

DEMO_PASSWORD = "CommerceLab123!"
# (code, name, city, share of stock, ISO country, region, market, opened)
WAREHOUSES = [("VAL", "Valencia Roastery", "Valencia", 0.6, "ES", "Comunidad Valenciana", "Iberia", "2019-04-01"),
              ("MAD", "Madrid Hub", "Madrid", 0.25, "ES", "Madrid", "Iberia", "2022-09-15"),
              ("BCN", "Barcelona Hub", "Barcelona", 0.15, "ES", "Cataluna", "Iberia", "2024-02-01")]
# (name, country, lead days, ISO code, currency, payment terms)
SUPPLIERS = [("Bensa Daye Exporters", "Ethiopia", 45, "ET", "USD", 45), ("Caravela Coffee", "Colombia", 40, "CO", "USD", 30),
             ("Dormans Kenya", "Kenya", 50, "KE", "USD", 45), ("Exportadora Cerrado", "Brazil", 35, "BR", "USD", 30),
             ("Descafecol", "Colombia", 30, "CO", "USD", 30), ("Tolva Brewware", "Portugal", 10, "PT", "EUR", 30),
             ("Norrgrind AB", "Sweden", 14, "SE", "SEK", 14), ("Kiln & Clay Studio", "Spain", 7, "ES", "EUR", 15)]


def money(value):
    return Decimal(str(value)).quantize(Decimal("0.01"))


class Command(BaseCommand):
    help = "Seed the specialty-coffee demo catalog, warehouses, suppliers, reviews and demo accounts"

    def add_arguments(self, parser):
        parser.add_argument("--seed", type=int, default=42)

    def handle(self, *args, **options):
        rng = random.Random(options["seed"])
        self._accounts()
        categories = {slug: self._category(slug, name, description, position, image)
                      for slug, name, description, position, image in CATEGORIES}
        brands = {slug: self._brand(slug, name, description) for slug, name, description in BRANDS}
        warehouses = [(Warehouse.objects.update_or_create(code=code, defaults={
            "name": name, "city": city, "country_code": iso, "region": region, "market": market,
            "timezone": "Europe/Madrid", "opened_on": opened})[0], share)
            for code, name, city, share, iso, region, market, opened in WAREHOUSES]
        suppliers = [Supplier.objects.update_or_create(name=name, defaults={
            "country": country, "default_lead_days": lead, "country_code": iso, "currency": currency,
            "payment_terms_days": terms, "contract_started_on": timezone.now().date() - timedelta(days=rng.randint(400, 2200)),
            "reliability_score": Decimal(str(rng.randint(82, 99)))})[0]
            for name, country, lead, iso, currency, terms in SUPPLIERS]
        self._subcategories(categories)
        tags = self._tags()

        created = 0
        for index, spec in enumerate(PRODUCTS):
            product, was_created = self._product(spec, categories, brands)
            created += was_created
            self._images(product, spec)
            self._variants(product, spec, warehouses, suppliers, rng)
            if was_created:  # stagger creation dates so "new arrivals" and "newest" sorting mean something
                age = spec.get("age_days", 40 + index * 7)
                Product.objects.filter(pk=product.pk).update(created_at=timezone.now() - timedelta(days=age))
        self._classify(tags, categories, rng)
        self._reviews()
        self._marketing()
        self._catalog_history(rng)
        self._supply(warehouses, suppliers, rng)
        self._snapshots(warehouses, rng)
        self._newsletter(rng)
        self.stdout.write(self.style.SUCCESS(
            f"Seeded {Product.objects.count()} products ({created} new, {Product.objects.storefront().count()} live), "
            f"{Tag.objects.count()} tags, {Category.objects.count()} categories, {Review.objects.count()} reviews, "
            f"{Campaign.objects.count()} campaigns, {Discount.objects.count()} discount codes, "
            f"{PurchaseOrder.objects.count()} purchase orders, {InventorySnapshot.objects.count()} stock snapshots; "
            f"demo password: {DEMO_PASSWORD}"))

    # ------------------------------------------------------------------ helpers
    def _accounts(self):
        for role in ["Customer", "Admin", "Store Manager", "Warehouse Manager", "Analyst", "Marketing Manager", "Support Manager"]:
            Group.objects.get_or_create(name=role)
        accounts = [("admin", "Admin", True, True, "Alex", "Admin"), ("manager", "Store Manager", True, False, "Marina", "Store"),
                    ("warehouse", "Warehouse Manager", True, False, "Walter", "Stock"), ("analyst", "Analyst", False, False, "Ana", "Lyst"),
                    ("customer", "Customer", False, False, "Carla", "Customer")]
        for username, role, staff, superuser, first, last in accounts:
            user, _ = User.objects.get_or_create(username=username, defaults={"email": f"{username}@example.test"})
            user.set_password(DEMO_PASSWORD)
            user.is_staff, user.is_superuser, user.first_name, user.last_name = staff, superuser, first, last
            user.save()
            user.groups.add(Group.objects.get(name=role))
            if role == "Customer":
                CustomerProfile.objects.get_or_create(user=user, defaults={"country": "Spain", "city": "Valencia", "acquisition_source": "organic"})
        self._grant_group_permissions()

    @staticmethod
    def _category(slug, name, description, position, image):
        category, _ = Category.objects.update_or_create(slug=slug, defaults={
            "name": name, "description": description, "position": position, "image_url": unsplash(image, 900)})
        return category

    @staticmethod
    def _brand(slug, name, description):
        brand, _ = Brand.objects.update_or_create(slug=slug, defaults={"name": name, "description": description})
        return brand

    @staticmethod
    def _product(spec, categories, brands):
        content = {
            "name": spec["name"], "kind": spec.get("kind", "COFFEE"), "short_description": spec.get("short", ""),
            "description": spec.get("description", ""), "featured": spec.get("featured", False),
            "compare_at_price": money(spec["compare_at"]) if spec.get("compare_at") else None,
            "origin": spec.get("origin", ""), "region": spec.get("region", ""), "producer": spec.get("producer", ""),
            "variety": spec.get("variety", ""), "process": spec.get("process", ""), "roast_level": spec.get("roast", ""),
            "altitude": spec.get("altitude", ""), "tasting_notes": spec.get("notes", ""), "brew_methods": spec.get("brew", ""),
            "highlights": "\n".join(spec.get("highlights", [])), "brew_guide": "\n".join(spec.get("brew_guide", [])),
            "origin_story": spec.get("story", ""),
            "story_image_url": unsplash(spec["story_image"], 1400) if spec.get("story_image") else "",
            "specs": spec.get("specs", {}),
        }
        product, created = Product.objects.get_or_create(sku=spec["sku"], defaults={
            **content, "slug": slugify(spec["name"]), "category": categories[spec["category"]],
            "brand": brands[spec.get("brand", "sondermark")], "purchase_cost": money(spec["cost"]), "sale_price": money(spec["price"]),
            "status": spec.get("status", "ACTIVE"), "tax_rate": 10 if spec.get("kind", "COFFEE") != "EQUIPMENT" else 21,
            "reorder_point": 12 if spec.get("kind", "COFFEE") == "COFFEE" else 5, "preferred_reorder_quantity": 60})
        if not created:  # refresh editorial content, but keep prices/visibility that staff may have changed in the admin
            for field, value in content.items():
                setattr(product, field, value)
            product.save()
        return product, created

    @staticmethod
    def _images(product, spec):
        if product.images.exists():
            return
        for position, (photo_id, alt) in enumerate(spec["images"]):
            ProductImage.objects.create(product=product, primary=(position == 0), position=position,
                                        url=unsplash(photo_id), alt_text=alt)

    @staticmethod
    def _variants(product, spec, warehouses, suppliers, rng):
        variants = spec.get("variants") or COFFEE_VARIANTS
        base_price = Decimal(str(spec["price"]))
        total_stock = spec.get("stock", 0)
        for suffix, name, multiplier, attributes in variants:
            price = None if multiplier == 1 else money(base_price * Decimal(str(multiplier)))
            variant, _ = ProductVariant.objects.get_or_create(sku=f"{spec['sku']}-{suffix}", defaults={
                "product": product, "name": name, "price": price, "attributes": attributes})
            weight = 1 if multiplier == 1 else 0.35  # bulk / long-cycle variants carry less stock
            for warehouse, share in warehouses:
                physical = int(round(total_stock * share * weight)) if total_stock else 0
                Inventory.objects.get_or_create(warehouse=warehouse, variant=variant, defaults={"physical": physical})
            supplier = suppliers[rng.randrange(len(suppliers))]
            SupplierProduct.objects.get_or_create(supplier=supplier, variant=variant, defaults={
                "supplier_sku": f"{supplier.id}-{variant.sku}", "purchase_price": money(spec["cost"]),
                "lead_days": supplier.default_lead_days})

    @staticmethod
    def _reviews():
        if Review.objects.exists():
            return
        customer_group = Group.objects.get(name="Customer")
        reviewers = []
        for first, last, city in REVIEWERS:
            username = f"{first}.{last}".lower().replace(" ", "")
            user, created = User.objects.get_or_create(username=username, defaults={
                "first_name": first, "last_name": last, "email": f"{username}@example.test"})
            if created:
                user.set_unusable_password()
                user.save()
                user.groups.add(customer_group)
                CustomerProfile.objects.get_or_create(user=user, defaults={"country": "EU", "city": city, "acquisition_source": "organic"})
            reviewers.append(user)
        for sku, entries in REVIEWS.items():
            product = Product.objects.filter(sku=sku).first()
            if not product:
                continue
            for reviewer_index, rating, days_ago, comment in entries:
                review = Review.objects.create(customer=reviewers[reviewer_index], product=product, rating=rating, comment=comment,
                                               status=Review.Status.APPROVED, verified_purchase=True)
                Review.objects.filter(pk=review.pk).update(created_at=timezone.now() - timedelta(days=days_ago))

    @staticmethod
    def _marketing():
        """Campaigns and promo codes, each carrying the reporting attributes analysts group by."""
        today = timezone.now().date()
        campaigns = {}
        for name, source, medium, utm, group, objective, spend, market, country in CAMPAIGNS:
            seasonal = objective == "PROMOTION"
            campaign, _ = Campaign.objects.update_or_create(name=name, defaults={
                "source": source, "medium": medium, "utm_campaign": utm, "channel_group": group,
                "objective": objective, "spend": spend, "budget": Decimal(str(spend)) * Decimal("1.15"),
                "target_market": market, "target_country_code": country, "currency": "EUR",
                "status": "RUNNING", "owner": "Marketing Manager",
                "start_date": today - timedelta(days=730),
                "end_date": today + timedelta(days=0 if seasonal else 120)})
            campaigns[utm] = campaign
        # Campaigns seeded by earlier versions have no reporting bucket; give them one so the
        # media endpoints can group every row.
        for campaign in Campaign.objects.filter(channel_group=""):
            campaign.channel_group = {"google": "Paid Search", "meta": "Paid Social", "email": "Email",
                                      "newsletter": "Email", "referral": "Referral",
                                      "affiliate": "Affiliate"}.get(campaign.source, "Other")
            campaign.save(update_fields=["channel_group"])
        for code, kind, value, minimum, channel, first_only, utm, description in DISCOUNTS:
            Discount.objects.update_or_create(code=code, defaults={
                "kind": kind, "value": value, "minimum_cart": minimum, "channel": channel,
                "first_order_only": first_only, "description": description, "name": code.title(),
                "campaign": campaigns.get(utm), "usage_limit": 100000,
                "starts_at": timezone.now() - timedelta(days=730),
                "ends_at": timezone.now() + timedelta(days=365)})

    @staticmethod
    def _subcategories(categories):
        """Second catalogue level. Children reuse the parent artwork so every category page has an image."""
        for slug, name, parent_slug, description, position in SUBCATEGORIES:
            parent = categories[parent_slug]
            categories[slug] = Category.objects.update_or_create(slug=slug, defaults={
                "name": name, "description": description, "position": position,
                "parent": parent, "image_url": parent.image_url})[0]

    @staticmethod
    def _tags():
        return {slug: Tag.objects.update_or_create(slug=slug, defaults={
            "name": name, "group": group, "description": description})[0]
            for slug, name, group, description in TAGS}

    @staticmethod
    def _classify(tags, categories, rng):
        """Attach tags and move coffees into the origin / style subcategory they belong to."""
        africa = {"ethiopia", "kenya", "rwanda", "burundi", "uganda", "tanzania"}
        asia = {"indonesia", "india", "papua", "vietnam", "sumatra"}
        for product in Product.objects.prefetch_related("tags"):
            notes = f"{product.name} {product.tasting_notes} {product.short_description}".lower()
            origin = (product.origin or "").split(",")[0].strip().lower()
            chosen = set()
            if product.featured:
                chosen.add("bestseller")
            if product.kind == "BUNDLE":
                chosen.update({"gift", "beginner-friendly"})
            if product.process.startswith("DECAF"):
                chosen.add("decaf")
            if any(word in notes for word in ("berry", "citrus", "peach", "floral", "jasmine", "fruit")):
                chosen.add("fruity")
            if any(word in notes for word in ("chocolate", "cocoa", "caramel", "nut", "hazelnut", "toffee")):
                chosen.add("chocolatey")
            if "cold_brew" in product.brew_method_codes or any(w in notes for w in ("cold brew", "iced")):
                chosen.add("summer")
            if product.kind in {"COFFEE", "SUBSCRIPTION"}:
                chosen.add("subscription-friendly")
                chosen.add("everyday" if product.sale_price <= 14 else "enthusiast")
            if product.margin_percent >= 55:
                chosen.add("high-margin")
            if product.kind == "COFFEE" and (product.process == "ANAEROBIC" or rng.random() < 0.18):
                chosen.add("limited")
            product.tags.set([tags[slug] for slug in chosen if slug in tags])

            # Origin and style subcategories, only for products still sitting on a parent category.
            parent = product.category.slug
            target = None
            if parent == "filter-coffee":
                target = "africa" if origin in africa else "asia-pacific" if origin in asia else "latin-america"
            elif parent == "espresso":
                target = "espresso-blends" if "blend" in product.name.lower() else "espresso-single-origin"
            elif parent == "equipment":
                specs = " ".join(str(v) for v in (product.specs or {}).values()).lower()
                if any(word in notes + specs for word in ("grinder", "kettle")):
                    target = "grinders-kettles"
                elif any(word in notes for word in ("mug", "cup", "scale", "filter", "brush")):
                    target = "mugs-accessories"
                else:
                    target = "brewers"
            updates = {}
            if target and target in categories:
                updates["category"] = categories[target]
            if not product.origin_country_code:
                updates["origin_country_code"] = {"ethiopia": "ET", "kenya": "KE", "colombia": "CO", "brazil": "BR",
                                                  "costa rica": "CR", "guatemala": "GT", "peru": "PE",
                                                  "rwanda": "RW", "indonesia": "ID"}.get(origin, "")
            if product.launched_at is None or product.launched_at > product.created_at.date():
                updates["launched_at"] = product.created_at.date()  # the signal stamps "today" on creation; backdate it
            if product.weight_grams is None and product.kind == "COFFEE":
                updates["weight_grams"] = 250
            updates["is_limited"] = "limited" in chosen
            updates["is_seasonal"] = "summer" in chosen or product.kind == "BUNDLE"
            Product.objects.filter(pk=product.pk).update(**updates)

    @staticmethod
    def _catalog_history(rng):
        """Backdated price and visibility changes, so 'what did it cost in March' is answerable."""
        if ProductStatusHistory.objects.filter(note="Published").exists():
            return  # already seeded: every seeded product gets a "Published" row below
        ProductStatusHistory.objects.all().delete()  # drop the rows the signal wrote on creation; rebuild a backdated timeline
        now = timezone.now()
        for index, product in enumerate(Product.objects.order_by("id")):
            born = product.created_at
            ProductStatusHistory.objects.create(product=product, from_status="", to_status="DRAFT",
                                                changed_at=born - timedelta(days=14), note="Created")
            ProductStatusHistory.objects.create(product=product, from_status="DRAFT", to_status="ACTIVE",
                                                changed_at=born, note="Published")
            if product.status == "DISCONTINUED":
                ProductStatusHistory.objects.create(product=product, from_status="ACTIVE", to_status="DISCONTINUED",
                                                    changed_at=now - timedelta(days=rng.randint(20, 200)),
                                                    note="Lot sold out")
            # Roughly two products in three have been repriced at least once, always after launch.
            age_days = max((now - born).days, 1)
            for step in range(rng.choice([0, 1, 1, 2])):
                if age_days < 25:
                    break
                changed_at = now - timedelta(days=rng.randint(10, age_days - 10) - step * 5)
                if changed_at < born:
                    continue
                factor = Decimal(str(rng.choice([0.92, 0.95, 1.06, 1.09, 1.15])))
                old = (product.sale_price / factor).quantize(Decimal("0.01"))
                PriceHistory.objects.filter(pk=PriceHistory.objects.create(
                    product=product, old_price=old, new_price=product.sale_price).pk).update(changed_at=changed_at)

    @staticmethod
    def _supply(warehouses, suppliers, rng):
        """Purchase orders and inter-warehouse transfers — the inbound side of the stock ledger."""
        if PurchaseOrder.objects.exists():
            return
        variants = list(ProductVariant.objects.select_related("product").order_by("id"))
        now = timezone.now()
        for index in range(48):
            supplier = suppliers[index % len(suppliers)]
            warehouse = warehouses[index % len(warehouses)][0]
            raised = now - timedelta(days=rng.randint(10, 700))
            status = rng.choices(["RECEIVED", "PARTIALLY_RECEIVED", "CONFIRMED", "SUBMITTED", "CANCELLED", "DRAFT"],
                                 [0.62, 0.10, 0.10, 0.08, 0.06, 0.04])[0]
            lead = supplier.default_lead_days
            order = PurchaseOrder.objects.create(
                supplier=supplier, warehouse=warehouse, status=status,
                reference=f"PO-{raised:%Y%m}-{index:03d}", currency=supplier.currency,
                fx_rate=Decimal("0.92") if supplier.currency == "USD" else Decimal("1.000000"),
                freight_cost=Decimal(str(rng.randint(60, 480))),
                expected_delivery=(raised + timedelta(days=lead)).date(),
                submitted_at=raised if status != "DRAFT" else None,
                confirmed_at=raised + timedelta(days=1) if status in
                {"CONFIRMED", "PARTIALLY_RECEIVED", "RECEIVED"} else None,
                # Late deliveries are what makes supplier reliability worth analysing.
                received_at=raised + timedelta(days=lead + rng.choice([-2, 0, 1, 3, 9, 21]))
                if status in {"RECEIVED", "PARTIALLY_RECEIVED"} else None,
                cancelled_at=raised + timedelta(days=rng.randint(1, 12)) if status == "CANCELLED" else None)
            PurchaseOrder.objects.filter(pk=order.pk).update(created_at=raised)
            for variant in rng.sample(variants, rng.randint(1, 5)):
                quantity = rng.choice([40, 60, 90, 120, 240])
                received = quantity if status == "RECEIVED" else int(quantity * 0.6) if status == "PARTIALLY_RECEIVED" else 0
                PurchaseOrderItem.objects.create(purchase_order=order, variant=variant, quantity=quantity,
                                                 received_quantity=received, unit_cost=variant.product.purchase_cost)
        for index in range(24):
            source, destination = rng.sample([w for w, _ in warehouses], 2)
            raised = now - timedelta(days=rng.randint(5, 500))
            status = rng.choices(["RECEIVED", "IN_TRANSIT", "DRAFT", "CANCELLED"], [0.72, 0.14, 0.08, 0.06])[0]
            transfer = Transfer.objects.create(
                source=source, destination=destination, variant=rng.choice(variants),
                quantity=rng.choice([10, 20, 35, 50]), status=status,
                reason=rng.choice(["rebalance", "stockout cover", "promotion", ""]),
                dispatched_at=raised + timedelta(days=1) if status in {"IN_TRANSIT", "RECEIVED"} else None,
                received_at=raised + timedelta(days=rng.randint(2, 6)) if status == "RECEIVED" else None)
            Transfer.objects.filter(pk=transfer.pk).update(created_at=raised)

    @staticmethod
    def _snapshots(warehouses, rng, weeks=78):
        """Weekly stock photographs: current Inventory only knows today."""
        if InventorySnapshot.objects.exists():
            return
        today = timezone.now().date()
        rows, levels = [], {}
        for inventory in Inventory.objects.select_related("variant__product", "warehouse"):
            levels[(inventory.warehouse_id, inventory.variant_id)] = inventory
        for week in range(weeks):
            day = today - timedelta(days=week * 7)
            for (warehouse_id, variant_id), inventory in levels.items():
                # Walk backwards from today with a random drift, so the series has a shape.
                drift = 1 + (week * rng.uniform(0.004, 0.02))
                physical = max(0, int(inventory.physical * drift * rng.uniform(0.75, 1.25)))
                rows.append(InventorySnapshot(
                    snapshot_date=day, warehouse_id=warehouse_id, variant_id=variant_id, physical=physical,
                    reserved=min(physical, int(physical * rng.uniform(0, 0.14))),
                    incoming=rng.choice([0, 0, 0, 30, 60, 120]),
                    damaged=rng.choice([0, 0, 0, 0, 1, 2, 5]),
                    unit_cost=inventory.variant.product.purchase_cost))
        InventorySnapshot.objects.bulk_create(rows, batch_size=2000, ignore_conflicts=True)

    @staticmethod
    def _newsletter(rng, count=320):
        """List signups that only partly become customers — the top of the e-mail funnel."""
        if NewsletterSubscriber.objects.exists():
            return
        now = timezone.now()
        sources = [("footer", 0.42), ("popup", 0.24), ("checkout", 0.18), ("event", 0.09), ("partner", 0.07)]
        rows = []
        for index in range(count):
            created = now - timedelta(days=rng.randint(1, 730), hours=rng.randint(0, 23))
            confirmed = rng.random() < 0.71
            rows.append(NewsletterSubscriber(
                email=f"subscriber.{index:04d}@example.com",
                source=rng.choices([s for s, _ in sources], [w for _, w in sources])[0],
                medium="email", campaign=rng.choice(["", "", "welcome_flow", "weekly_roast_note"]),
                country_code=rng.choice(["ES", "ES", "FR", "DE", "IT", "PT", "NL", "SE", "GB", ""]),
                active=rng.random() < 0.88,
                confirmed_at=created + timedelta(hours=rng.randint(1, 72)) if confirmed else None,
                unsubscribed_at=created + timedelta(days=rng.randint(10, 400)) if rng.random() < 0.12 else None))
        created_rows = NewsletterSubscriber.objects.bulk_create(rows, batch_size=500, ignore_conflicts=True)
        for row in created_rows:
            if row.pk:
                NewsletterSubscriber.objects.filter(pk=row.pk).update(
                    created_at=(row.confirmed_at or now) - timedelta(hours=rng.randint(1, 72)))

    @staticmethod
    def _grant_group_permissions():
        from django.contrib.auth.models import Permission
        grants = {"Store Manager": ["catalog", "sales", "accounts"], "Warehouse Manager": ["operations"],
                  "Admin": ["catalog", "sales", "operations", "accounts", "auth"]}
        for group_name, app_labels in grants.items():
            Group.objects.get(name=group_name).permissions.add(*Permission.objects.filter(content_type__app_label__in=app_labels))
