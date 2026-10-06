"""Send one SMS through the configured gateway, straight from the terminal.

Useful while setting credentials up: it reports which provider is in play and what
the gateway answered, without going through login and the settings page.

    python manage.py send_test_sms 09171234567
    python manage.py send_test_sms --user juaquin
    python manage.py send_test_sms 09171234567 --message "Aphids detected"
"""

from django.conf import settings
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError

from predictions.notifications import (
    normalize_phone_number,
    send_sms_notification,
    sms_is_configured,
    sms_provider,
)

DEFAULT_MESSAGE = 'HI-TECH EGGPLANT: test message. Your SMS gateway is working.'


class Command(BaseCommand):
    help = 'Send a test SMS through the configured gateway (Twilio, Semaphore, or console).'

    def add_arguments(self, parser):
        parser.add_argument(
            'number',
            nargs='?',
            help='Mobile number to text, e.g. 09171234567. Omit when using --user.',
        )
        parser.add_argument(
            '--user',
            dest='username',
            help='Send to the number saved in this account\'s notification settings.',
        )
        parser.add_argument(
            '--message',
            default=DEFAULT_MESSAGE,
            help='Text to send. Defaults to a short test message.',
        )

    def handle(self, *args, **options):
        number = options.get('number')
        username = options.get('username')

        if not number and not username:
            raise CommandError('Give a number, or --user <username>.')

        if username:
            try:
                user = User.objects.get(username=username)
            except User.DoesNotExist:
                raise CommandError('No user named "{}".'.format(username))

            preference = getattr(user, 'notification_preference', None)
            if preference is None or not preference.phone_number:
                raise CommandError(
                    'User "{}" has no mobile number saved. Add one at /notifications/.'.format(username))
            number = preference.phone_number

            if not preference.sms_enabled:
                self.stdout.write(self.style.WARNING(
                    'Note: SMS alerts are switched off for this user, so real alerts will not be '
                    'texted until they turn the channel on. Sending the test anyway.'))

        provider = sms_provider()
        self.stdout.write('Provider:  {}'.format(provider))
        self.stdout.write('Configured: {}'.format('yes' if sms_is_configured() else 'no'))

        normalized = normalize_phone_number(number)
        if not normalized:
            raise CommandError(
                '"{}" is not a usable number. Use 09171234567 or +639171234567.'.format(number))
        self.stdout.write('To:        {} (from "{}")'.format(normalized, number))

        if provider == 'console':
            self.stdout.write(self.style.WARNING(
                'No gateway is configured, so the message will only be written to the log. '
                'Set TWILIO_ACCOUNT_SID/TWILIO_AUTH_TOKEN/TWILIO_FROM_NUMBER, or SEMAPHORE_API_KEY, '
                'to send for real.'))

        if not getattr(settings, 'NOTIFICATIONS_ENABLED', True):
            self.stdout.write(self.style.WARNING(
                'NOTIFICATIONS_ENABLED is 0, so nothing will be sent.'))

        sent, detail = send_sms_notification(normalized, options['message'])

        if sent:
            self.stdout.write(self.style.SUCCESS(detail))
        else:
            # A failed send is the answer the operator asked for, not a crash.
            self.stdout.write(self.style.ERROR(detail))
