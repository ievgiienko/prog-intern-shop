from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver
from django.utils import timezone

from .models import PriceHistory, Product, ProductStatusHistory, Review


@receiver(pre_save, sender=Product)
def record_price_change(sender, instance, **kwargs):
    """Every sale-price change (admin, API, shell) leaves a PriceHistory row. One rule, one place."""
    if not instance.pk:
        return
    previous = Product.objects.filter(pk=instance.pk).values_list("sale_price", flat=True).first()
    if previous is not None and previous != instance.sale_price:
        PriceHistory.objects.create(product=instance, old_price=previous, new_price=instance.sale_price)


def _stamp_lifecycle_dates(product):
    today = timezone.localdate()
    if product.status == Product.Status.ACTIVE:
        if product.launched_at is None:
            product.launched_at = today
        product.discontinued_at = None
    elif product.status == Product.Status.DISCONTINUED and product.discontinued_at is None:
        product.discontinued_at = today


@receiver(pre_save, sender=Product)
def record_status_change(sender, instance, **kwargs):
    """Every status change (admin, API, shell) leaves a history row and stamps launched_at / discontinued_at."""
    if not instance.pk:
        _stamp_lifecycle_dates(instance)
        return
    previous = Product.objects.filter(pk=instance.pk).values_list("status", flat=True).first()
    if previous is None or previous == instance.status:
        return
    _stamp_lifecycle_dates(instance)
    ProductStatusHistory.objects.create(
        product=instance, from_status=previous, to_status=instance.status, changed_at=timezone.now())


@receiver(post_save, sender=Product)
def record_initial_status(sender, instance, created, **kwargs):
    if created:
        ProductStatusHistory.objects.create(
            product=instance, from_status="", to_status=instance.status, note="Created", changed_at=timezone.now())


@receiver(pre_save, sender=Review)
def stamp_moderation_time(sender, instance, **kwargs):
    """moderated_at is set the moment a review leaves PENDING (approved or rejected)."""
    if not instance.pk or instance.moderated_at is not None:
        return
    previous = Review.objects.filter(pk=instance.pk).values_list("status", flat=True).first()
    if previous == Review.Status.PENDING and instance.status != Review.Status.PENDING:
        instance.moderated_at = timezone.now()