"""Report whether "Continue with Google" is ready, and what is still missing.

    python manage.py check_google_login

Checks only what this machine can know: that a client ID is configured, that it
looks like a Google one, and that google-auth is importable. Whether the origin is
registered can only be found out by clicking the button - the command prints the
origins to register so they can be compared by eye.
"""

from django.conf import settings
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand

from predictions.google_auth import google_login_is_configured
from predictions.models import GoogleIdentity


class Command(BaseCommand):
    help = 'Check that Sign in with Google is configured.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--port',
            default='8000',
            help='Port the dev server runs on, used to print the origins to register.',
        )

    def handle(self, *args, **options):
        client_id = getattr(settings, 'GOOGLE_OAUTH_CLIENT_ID', '')

        try:
            from google.oauth2 import id_token  # noqa: F401
            google_auth_available = True
        except ImportError:
            google_auth_available = False

        self.stdout.write('google-auth installed : {}'.format('yes' if google_auth_available else 'NO'))
        self.stdout.write('client ID configured  : {}'.format('yes' if client_id else 'NO'))

        if client_id:
            shown = client_id if len(client_id) < 24 else client_id[:12] + '...' + client_id[-24:]
            self.stdout.write('client ID             : {}'.format(shown))
            if not client_id.endswith('.apps.googleusercontent.com'):
                self.stdout.write(self.style.WARNING(
                    'That does not look like a Google client ID - they end in '
                    '".apps.googleusercontent.com".'))

        self.stdout.write('verified e-mail only  : {}'.format(
            'yes' if getattr(settings, 'GOOGLE_REQUIRE_VERIFIED_EMAIL', True) else 'no'))

        linked = GoogleIdentity.objects.count()
        self.stdout.write('linked Google accounts: {}'.format(linked))
        for identity in GoogleIdentity.objects.select_related('user')[:10]:
            self.stdout.write('  - {} -> {}'.format(identity.email or identity.sub, identity.user.username))

        self.stdout.write('')

        if not google_login_is_configured():
            self.stdout.write(self.style.ERROR('Not ready: the button will not be shown.'))
            if not client_id:
                self.stdout.write(
                    'Put your client ID in the .env file next to manage.py:\n'
                    '    GOOGLE_OAUTH_CLIENT_ID=1234567890-abcdef.apps.googleusercontent.com\n'
                    'then restart the server.')
            return

        self.stdout.write(self.style.SUCCESS('Ready: the button will appear on the login and signup pages.'))
        self.stdout.write('')
        self.stdout.write('Register these under "Authorized JavaScript origins" in the Google console')
        self.stdout.write('(a missing origin is the usual cause of a button that does nothing):')
        port = options['port']
        for host in ('localhost', '127.0.0.1'):
            self.stdout.write('    http://{}:{}'.format(host, port))

        # A user whose e-mail matches a Google account will be linked to it rather
        # than getting a second account, which is worth knowing before the first try.
        with_email = User.objects.exclude(email='').count()
        self.stdout.write('')
        self.stdout.write(
            '{} existing account(s) have an e-mail address; signing in with a Google account '
            'that matches one links to it instead of creating a new account.'.format(with_email))
