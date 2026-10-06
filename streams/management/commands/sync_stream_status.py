from django.core.management.base import BaseCommand, CommandError

from streams.services import sync_stream_states


class Command(BaseCommand):
    help = "Synchronize stream live flags using MediaMTX API path status."

    def handle(self, *args, **options):
        try:
            changed = sync_stream_states()
        except RuntimeError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(self.style.SUCCESS(f"Synced streams. Changed: {len(changed)}"))
