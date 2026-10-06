"""Sign in with Google, verified server-side.

The browser never tells us who the user is: it hands over an ID token that Google
signed, and `verify_google_token` checks that signature, the audience (our client
ID) and the expiry before anything is trusted. A token that fails any of those is
just a string a page posted at us.

google-auth is already installed as a dependency of firebase-admin, so this adds
no new package to the project.
"""

from __future__ import annotations

import logging
import re

from django.conf import settings
from django.contrib.auth.models import User
from django.db import transaction
from django.utils import timezone

try:
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token as google_id_token
except ImportError:  # pragma: no cover - only when google-auth is absent
    google_requests = None
    google_id_token = None

logger = logging.getLogger(__name__)

# Google mints ID tokens with one of these two issuers; anything else is not Google.
GOOGLE_ISSUERS = ('accounts.google.com', 'https://accounts.google.com')


class GoogleAuthError(Exception):
    """A sign-in attempt that could not be trusted, with a message safe to show the user."""


def google_login_is_configured():
    return bool(getattr(settings, 'GOOGLE_OAUTH_CLIENT_ID', '')) and google_id_token is not None


def verify_google_token(credential):
    """Return Google's claims for a credential, or raise GoogleAuthError.

    Everything this function returns has been checked against Google's public keys.
    """
    if not credential:
        raise GoogleAuthError('No Google credential was received.')

    if google_id_token is None:
        raise GoogleAuthError('Google sign-in is unavailable on this server (google-auth is missing).')

    client_id = getattr(settings, 'GOOGLE_OAUTH_CLIENT_ID', '')
    if not client_id:
        raise GoogleAuthError('Google sign-in is not configured on this server.')

    try:
        claims = google_id_token.verify_oauth2_token(
            credential, google_requests.Request(), client_id)
    except ValueError as exc:
        # Bad signature, wrong audience, expired token - all arrive as ValueError.
        logger.warning('Rejected a Google credential: %s', exc)
        raise GoogleAuthError('That Google sign-in could not be verified. Please try again.')
    except Exception as exc:
        logger.exception('Google token verification failed')
        raise GoogleAuthError('Could not reach Google to verify the sign-in. Please try again.')

    if claims.get('iss') not in GOOGLE_ISSUERS:
        raise GoogleAuthError('That sign-in did not come from Google.')

    if not claims.get('sub'):
        raise GoogleAuthError('Google did not identify the account.')

    email = (claims.get('email') or '').strip().lower()
    if not email:
        raise GoogleAuthError('That Google account has no e-mail address to sign in with.')

    if getattr(settings, 'GOOGLE_REQUIRE_VERIFIED_EMAIL', True) and not claims.get('email_verified'):
        raise GoogleAuthError('Google has not verified that e-mail address, so it cannot be used to sign in.')

    return claims


def _unique_username(preferred):
    """Turn an e-mail local part into a free username."""
    base = re.sub(r'[^\w.@+-]', '', (preferred or '').split('@')[0]).strip('.-_') or 'farmer'
    base = base[:140]

    if not User.objects.filter(username=base).exists():
        return base

    for suffix in range(1, 1000):
        candidate = '{}{}'.format(base[:140 - len(str(suffix))], suffix)
        if not User.objects.filter(username=candidate).exists():
            return candidate

    raise GoogleAuthError('Could not create a username for that Google account.')


@transaction.atomic
def get_or_create_user_from_google(claims):
    """Find the account this Google user signs into, creating one on first visit.

    Returns (user, created). Matching is by Google's `sub` first, then by verified
    e-mail so someone who already has a password account keeps the same data when
    they switch to the Google button.
    """
    from .models import GoogleIdentity

    sub = claims['sub']
    email = (claims.get('email') or '').strip().lower()
    given_name = (claims.get('given_name') or '')[:150]
    family_name = (claims.get('family_name') or '')[:150]
    picture = (claims.get('picture') or '')[:500]

    identity = GoogleIdentity.objects.filter(sub=sub).select_related('user').first()
    created = False

    if identity:
        user = identity.user
        # The address on the Google account can change; keep ours in step.
        if email and user.email != email:
            user.email = email
            user.save(update_fields=['email'])
    else:
        user = User.objects.filter(email__iexact=email).first() if email else None

        if user is None:
            user = User.objects.create(
                username=_unique_username(email),
                email=email,
                first_name=given_name,
                last_name=family_name,
            )
            # No password was chosen, so none should work: this account signs in
            # through Google until the user sets a password themselves.
            user.set_unusable_password()
            user.save(update_fields=['password'])
            created = True
        else:
            # Fill in a name we did not have, but never overwrite one the user set.
            fields = []
            if given_name and not user.first_name:
                user.first_name = given_name
                fields.append('first_name')
            if family_name and not user.last_name:
                user.last_name = family_name
                fields.append('last_name')
            if fields:
                user.save(update_fields=fields)

        identity = GoogleIdentity.objects.create(user=user, sub=sub, email=email, picture_url=picture)

    if not user.is_active:
        raise GoogleAuthError('That account is disabled. Contact the administrator.')

    identity.email = email or identity.email
    identity.picture_url = picture or identity.picture_url
    identity.last_login_at = timezone.now()
    identity.save(update_fields=['email', 'picture_url', 'last_login_at'])

    return user, created
