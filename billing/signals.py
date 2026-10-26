from django.db.models.signals import post_save
from django.dispatch import receiver
from .models import User, Wallet


@receiver(post_save, sender=User)
def create_wallet_for_new_user(sender, instance, created, **kwargs):
    """Every account starts with a zero-balance wallet."""
    if created:
        Wallet.objects.get_or_create(user=instance)
