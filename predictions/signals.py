from django.contrib.auth import get_user_model
from django.db.models.signals import post_save
from django.dispatch import receiver

from .firestore import sync_user_to_firestore
from .models import NotificationPreference


User = get_user_model()


@receiver(post_save, sender=User)
def sync_user_to_firestore_signal(sender, instance, **kwargs):
    try:
        sync_user_to_firestore(instance)
    except Exception:
        pass


@receiver(post_save, sender=User)
def create_notification_preference(sender, instance, created, **kwargs):
    """Give every new account a settings row so the alert pages never 404 on a missing profile."""
    if not created:
        return
    try:
        NotificationPreference.objects.get_or_create(user=instance)
    except Exception:
        pass
