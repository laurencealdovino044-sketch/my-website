from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from predictions.firestore import (
    get_firestore_client,
    sync_activity_history,
    sync_scheduled_application,
    sync_user_to_firestore,
)
from predictions.models import ActivityHistory, ScheduledApplication


class Command(BaseCommand):
    help = 'Migrate existing SQLite data into Firestore.'

    def handle(self, *args, **options):
        if get_firestore_client() is None:
            raise CommandError(
                'Firestore is not configured. Set GOOGLE_APPLICATION_CREDENTIALS or FIREBASE_CREDENTIALS_JSON first.'
            )

        user_model = get_user_model()
        user_count = 0
        activity_count = 0
        application_count = 0

        for user in user_model.objects.all().iterator():
            sync_user_to_firestore(user)
            user_count += 1

        for activity in ActivityHistory.objects.select_related('user').all().iterator():
            sync_activity_history(activity)
            activity_count += 1

        for application in ScheduledApplication.objects.select_related('user').all().iterator():
            sync_scheduled_application(application)
            application_count += 1

        self.stdout.write(
            self.style.SUCCESS(
                f'Synced {user_count} users, {activity_count} activity records, and {application_count} scheduled applications to Firestore.'
            )
        )
