from django.db import models
from django.contrib.auth.models import User

class ActivityHistory(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    action = models.CharField(max_length=100)  # e.g., 'manual_water', 'manual_spray', 'detection', etc.
    timestamp = models.DateTimeField(auto_now_add=True)
    details = models.TextField(blank=True, null=True)  # Additional details like duration, status, prediction result

    def __str__(self):
        return f"{self.user.username} - {self.action} at {self.timestamp}"

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        try:
            from .firestore import sync_activity_history

            sync_activity_history(self)
        except Exception:
            pass

    class Meta:
        ordering = ['-timestamp']

class ScheduledApplication(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    application_type = models.CharField(max_length=50, choices=[
        ('fertilizer', 'Fertilizer Application'),
        ('chemical', 'Chemical Application')
    ])
    plant_age = models.IntegerField()
    deficiency = models.CharField(max_length=50, default='none')
    recommendations = models.TextField()
    due_date = models.DateField()
    completed = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user.username} - {self.application_type} due {self.due_date}"

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        try:
            from .firestore import sync_scheduled_application

            sync_scheduled_application(self)
        except Exception:
            pass

    class Meta:
        ordering = ['due_date']


class NotificationPreference(models.Model):
    """Per-user alert settings: where to reach the farmer and what is worth interrupting them for."""

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='notification_preference')
    phone_number = models.CharField(
        max_length=20,
        blank=True,
        help_text='Mobile number for SMS alerts, e.g. 09171234567 or +639171234567.',
    )
    email_enabled = models.BooleanField(default=True)
    sms_enabled = models.BooleanField(default=False)
    web_enabled = models.BooleanField(
        default=True,
        help_text='Show alerts in the dashboard bell. Desktop pop-ups are a separate browser permission.',
    )
    notify_on_detection = models.BooleanField(default=True)
    notify_healthy_results = models.BooleanField(
        default=False,
        help_text='Also alert when a scan comes back healthy. Off by default so alerts stay meaningful.',
    )
    notify_on_schedule = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'Notification settings for {self.user.username}'

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        try:
            from .firestore import sync_notification_preference

            sync_notification_preference(self)
        except Exception:
            pass


class Notification(models.Model):
    """One alert shown inside the dashboard (the bell), independent of e-mail/SMS delivery.

    Kept even after it is read so the inbox doubles as a record of what the system
    told the farmer and when.
    """

    class Category(models.TextChoices):
        DETECTION = 'detection', 'Disease detection'
        SCHEDULE = 'schedule', 'Scheduled application'
        SYSTEM = 'system', 'System'
        TEST = 'test', 'Test'

    class Level(models.TextChoices):
        INFO = 'info', 'Info'
        SUCCESS = 'success', 'Success'
        WARNING = 'warning', 'Warning'
        DANGER = 'danger', 'Urgent'

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='notifications')
    title = models.CharField(max_length=150)
    message = models.TextField(blank=True)
    category = models.CharField(max_length=20, choices=Category.choices, default=Category.SYSTEM)
    level = models.CharField(max_length=10, choices=Level.choices, default=Level.INFO)
    link_url = models.CharField(max_length=300, blank=True)
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    read_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        # -id breaks ties: auto_now_add can stamp two notifications with the same
        # timestamp, and the bell relies on the newest one being first.
        ordering = ['-created_at', '-id']
        indexes = [
            # The bell polls "my unread, newest first" on every page, so index that path.
            models.Index(fields=['user', 'is_read', '-created_at']),
        ]

    def __str__(self):
        return f'{self.user.username}: {self.title}'

    def mark_read(self):
        if self.is_read:
            return
        from django.utils import timezone as _timezone

        self.is_read = True
        self.read_at = _timezone.now()
        self.save(update_fields=['is_read', 'read_at'])


class GoogleIdentity(models.Model):
    """Links a Django account to the Google account that signs into it.

    Keyed on Google's `sub` claim rather than the e-mail address: `sub` is stable
    for the life of the Google account, while an e-mail can be changed or reused.
    """

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='google_identity')
    sub = models.CharField(max_length=64, unique=True, db_index=True)
    email = models.EmailField(blank=True)
    picture_url = models.URLField(blank=True, max_length=500)
    created_at = models.DateTimeField(auto_now_add=True)
    last_login_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        verbose_name_plural = 'Google identities'

    def __str__(self):
        return f'{self.user.username} <- {self.email or self.sub}'
