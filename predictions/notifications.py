"""Outbound alerts for Hi-Tech Eggplant: Gmail e-mail and SMS.

Both channels are best-effort. A farm alert must never be the reason a scan or a
schedule fails to save, so every public function here catches its own errors and
reports them through the return value instead of raising - the same defensive
style used by predictions.firestore.

Configuration lives in hi_tech/settings.py and comes from environment variables;
nothing here reads credentials from the database or from user input.
"""

from __future__ import annotations

import logging
import re
import threading

import requests
from django.conf import settings
from django.core.mail import EmailMultiAlternatives, get_connection

logger = logging.getLogger(__name__)

TWILIO_API_ROOT = 'https://api.twilio.com/2010-04-01'
SEMAPHORE_API_URL = 'https://api.semaphore.co/api/v4/messages'

# A single GSM part is 160 characters. Longer messages still send, but each extra
# part is billed separately, so alerts are trimmed before they are handed over.
SMS_MAX_LENGTH = 300


def notifications_enabled():
    return bool(getattr(settings, 'NOTIFICATIONS_ENABLED', True))


def email_is_configured():
    """True when mail can actually leave the machine (or is being echoed to the console)."""
    backend = getattr(settings, 'EMAIL_BACKEND', '')
    if 'smtp' not in backend:
        return True  # console/locmem/dummy backends never fail on missing credentials
    return bool(getattr(settings, 'EMAIL_HOST_USER', '') and getattr(settings, 'EMAIL_HOST_PASSWORD', ''))


def sms_provider():
    return (getattr(settings, 'SMS_PROVIDER', 'console') or 'console').lower()


def sms_is_configured():
    provider = sms_provider()
    if provider == 'twilio':
        return bool(
            getattr(settings, 'TWILIO_ACCOUNT_SID', '')
            and getattr(settings, 'TWILIO_AUTH_TOKEN', '')
            and getattr(settings, 'TWILIO_FROM_NUMBER', '')
        )
    if provider == 'semaphore':
        return bool(getattr(settings, 'SEMAPHORE_API_KEY', ''))
    return provider == 'console'


def normalize_phone_number(raw):
    """Return an E.164 number, or an empty string when the input cannot be one.

    Farmers type their number the way they say it - 0917 123 4567 - so the local
    trunk prefix is swapped for the configured country code. Numbers already in
    international form (+63..., 0063...) are passed through untouched.
    """
    if not raw:
        return ''

    digits = re.sub(r'[^\d+]', '', str(raw))
    if not digits:
        return ''

    country_code = (getattr(settings, 'SMS_DEFAULT_COUNTRY_CODE', '+63') or '+63').strip()
    if not country_code.startswith('+'):
        country_code = '+' + country_code

    if digits.startswith('+'):
        candidate = digits
    elif digits.startswith('00'):
        candidate = '+' + digits[2:]
    elif digits.startswith('0'):
        candidate = country_code + digits[1:]
    elif digits.startswith(country_code.lstrip('+')):
        candidate = '+' + digits
    else:
        candidate = country_code + digits

    # E.164 allows at most 15 digits after the plus sign.
    if not re.fullmatch(r'\+\d{8,15}', candidate):
        return ''
    return candidate


def _trim_sms(message):
    message = ' '.join((message or '').split())
    if len(message) <= SMS_MAX_LENGTH:
        return message
    return message[: SMS_MAX_LENGTH - 3].rstrip() + '...'


def send_email_notification(to_email, subject, message, html_message=None):
    """Send one e-mail through the configured Gmail SMTP account.

    Returns (sent, detail); detail carries a short reason when sent is False.
    """
    if not notifications_enabled():
        return False, 'Notifications are turned off on the server.'
    if not to_email:
        return False, 'No e-mail address on file for this account.'
    if not email_is_configured():
        return False, 'Gmail is not configured (set GMAIL_ADDRESS and GMAIL_APP_PASSWORD).'

    try:
        connection = get_connection(timeout=getattr(settings, 'EMAIL_TIMEOUT', 15))
        mail = EmailMultiAlternatives(
            subject=subject,
            body=message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[to_email],
            connection=connection,
        )
        if html_message:
            mail.attach_alternative(html_message, 'text/html')
        mail.send(fail_silently=False)
        return True, 'E-mail sent to ' + to_email + '.'
    except Exception as exc:
        logger.exception('Failed to send notification e-mail to %s', to_email)
        return False, 'E-mail failed: {}'.format(exc)


def _send_sms_twilio(phone, message):
    response = requests.post(
        '{}/Accounts/{}/Messages.json'.format(TWILIO_API_ROOT, settings.TWILIO_ACCOUNT_SID),
        auth=(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN),
        data={'From': settings.TWILIO_FROM_NUMBER, 'To': phone, 'Body': message},
        timeout=getattr(settings, 'SMS_TIMEOUT', 15),
    )
    if response.status_code in (200, 201):
        return True, 'SMS sent to {} via Twilio.'.format(phone)

    detail = response.text
    try:
        detail = response.json().get('message', detail)
    except ValueError:
        pass
    return False, 'Twilio rejected the message ({}): {}'.format(response.status_code, detail)


def _send_sms_semaphore(phone, message):
    payload = {
        'apikey': settings.SEMAPHORE_API_KEY,
        'number': phone,
        'message': message,
    }
    if getattr(settings, 'SEMAPHORE_SENDER_NAME', ''):
        payload['sendername'] = settings.SEMAPHORE_SENDER_NAME

    response = requests.post(SEMAPHORE_API_URL, data=payload, timeout=getattr(settings, 'SMS_TIMEOUT', 15))
    if response.status_code in (200, 201):
        return True, 'SMS sent to {} via Semaphore.'.format(phone)
    return False, 'Semaphore rejected the message ({}): {}'.format(response.status_code, response.text)


def send_sms_notification(raw_phone, message):
    """Send one SMS through the configured gateway. Returns (sent, detail)."""
    if not notifications_enabled():
        return False, 'Notifications are turned off on the server.'

    phone = normalize_phone_number(raw_phone)
    if not phone:
        return False, 'No valid mobile number on file (use 09171234567 or +639171234567).'

    body = _trim_sms(message)
    provider = sms_provider()

    try:
        if provider == 'twilio':
            if not sms_is_configured():
                return False, 'Twilio is not configured (set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER).'
            return _send_sms_twilio(phone, body)

        if provider == 'semaphore':
            if not sms_is_configured():
                return False, 'Semaphore is not configured (set SEMAPHORE_API_KEY).'
            return _send_sms_semaphore(phone, body)

        # No gateway configured: log the message so the whole flow can still be tested.
        logger.info('[SMS console] To %s: %s', phone, body)
        return True, 'SMS gateway not configured - the message was logged for ' + phone + '.'
    except Exception as exc:
        logger.exception('SMS send failed for %s', phone)
        return False, 'SMS failed: {}'.format(exc)


def get_preference(user):
    """Return the user's NotificationPreference row, creating it on first use."""
    from .models import NotificationPreference

    preference, _ = NotificationPreference.objects.get_or_create(user=user)
    return preference


def create_web_notification(user, title, message, category='system', level='info', link_url='',
                            preference=None):
    """Drop an alert into the dashboard bell. Returns (created, detail).

    This is a database write, so it always runs in the caller's thread - never in
    the background sender - to keep SQLite from locking against the open request.
    """
    if user is None or not notifications_enabled():
        return False, 'Notifications are turned off on the server.'

    try:
        preference = preference or get_preference(user)
    except Exception:
        logger.exception('Could not load notification preferences for user %s', getattr(user, 'pk', None))
        return False, 'Could not load notification preferences.'

    if not preference.web_enabled:
        return False, 'In-app notifications are switched off for this account.'

    try:
        from .models import Notification

        Notification.objects.create(
            user=user,
            title=title[:150],
            message=message or '',
            category=category,
            level=level,
            link_url=link_url or '',
        )
        return True, 'Added to your notification bell.'
    except Exception as exc:
        logger.exception('Could not create in-app notification for user %s', getattr(user, 'pk', None))
        return False, 'In-app notification failed: {}'.format(exc)


def notify_user(user, subject, message, sms_message=None, html_message=None,
                category='system', level='info', link_url=''):
    """Send one alert on every channel the user has switched on.

    Returns {'in-app': (sent, detail) or None, 'email': ..., 'sms': ...}; a None
    entry means that channel was off, so nothing was attempted.
    """
    results = {'in-app': None, 'email': None, 'sms': None}
    if user is None or not notifications_enabled():
        return results

    try:
        preference = get_preference(user)
    except Exception:
        logger.exception('Could not load notification preferences for user %s', getattr(user, 'pk', None))
        return results

    if preference.web_enabled:
        results['in-app'] = create_web_notification(
            user, subject, message, category=category, level=level, link_url=link_url, preference=preference)

    if preference.email_enabled and user.email:
        results['email'] = send_email_notification(user.email, subject, message, html_message)

    if preference.sms_enabled and preference.phone_number:
        results['sms'] = send_sms_notification(
            preference.phone_number,
            sms_message or '{}\n{}'.format(subject, message),
        )

    return results


def notify_user_async(user, subject, message, sms_message=None, html_message=None, preference=None):
    """Send the alert from a daemon thread, without touching the database there.

    SMTP and the SMS gateways are network calls of a second or more each; sending
    them inline would add that to the response time of every scan. Callers that
    need the outcome (the "send test" button) call notify_user directly instead.

    Which channels to use is decided here, in the caller's thread, and only the
    plain strings are handed over: a worker writing to the database while the
    request that spawned it still holds the write lock is exactly how SQLite
    produces "database table is locked".
    """
    if user is None or not notifications_enabled():
        return

    try:
        preference = preference or get_preference(user)
    except Exception:
        logger.exception('Could not load notification preferences for user %s', getattr(user, 'pk', None))
        return

    email_target = user.email if (preference.email_enabled and user.email) else None
    phone_target = preference.phone_number if (preference.sms_enabled and preference.phone_number) else None
    if not email_target and not phone_target:
        return

    sms_body = sms_message or '{}\n{}'.format(subject, message)

    def _dispatch():
        if email_target:
            send_email_notification(email_target, subject, message, html_message)
        if phone_target:
            send_sms_notification(phone_target, sms_body)

    threading.Thread(target=_dispatch, daemon=True).start()


def build_detection_alert(user, predicted_class, confidence, disease_info=None, source='upload'):
    """Compose the (subject, body, sms) triple for a disease detection."""
    info = disease_info or {}
    source_label = {
        'upload': 'an uploaded photo',
        'live': 'the live drone feed',
    }.get(source, source)

    name = getattr(user, 'first_name', '') or getattr(user, 'username', 'Farmer')
    medicine = info.get('medicine') or []
    recommendation = info.get('recommendation') or 'Inspect the affected plants and treat them as soon as possible.'

    subject = 'Hi-Tech Eggplant alert: {} detected'.format(predicted_class)
    lines = [
        'Hello {},'.format(name),
        '',
        'A scan of {} detected {} with {}% confidence.'.format(source_label, predicted_class, confidence),
    ]
    if info.get('about'):
        lines += ['', info['about']]
    if medicine:
        lines += ['', '{}:'.format(info.get('medicine_label', 'Recommended treatment'))]
        lines += ['  - {}'.format(item) for item in medicine]
    lines += [
        '',
        'What to do: {}'.format(recommendation),
        '',
        'Open the dashboard for the full result and history.',
    ]
    body = '\n'.join(lines)

    sms = 'HI-TECH EGGPLANT: {} detected ({}% confidence) from {}. {}'.format(
        predicted_class, confidence, source_label, recommendation)
    return subject, body, sms


def notify_disease_detection(user, predicted_class, confidence, disease_info=None, source='upload'):
    """Alert the user about a detection, honouring their per-event preferences."""
    if user is None or not notifications_enabled():
        return

    try:
        preference = get_preference(user)
    except Exception:
        logger.exception('Could not load notification preferences for user %s', getattr(user, 'pk', None))
        return

    if not preference.notify_on_detection:
        return

    if predicted_class == 'Healthy Eggplant Leaf' and not preference.notify_healthy_results:
        return

    subject, body, sms = build_detection_alert(user, predicted_class, confidence, disease_info, source)

    healthy = predicted_class == 'Healthy Eggplant Leaf'
    # The bell entry is short on purpose - the full advice is in the e-mail and on the
    # result page; the panel only has to say what was found and how sure the model was.
    create_web_notification(
        user,
        '{} detected'.format(predicted_class),
        '{}% confidence from {}.'.format(
            confidence, 'the live drone feed' if source == 'live' else 'an uploaded photo'),
        category='detection',
        level='success' if healthy else 'danger',
        link_url='/result/' if source == 'upload' else '/predict/',
        preference=preference,
    )
    notify_user_async(user, subject, body, sms_message=sms, preference=preference)


def notify_scheduled_application(user, application):
    """Confirm a newly scheduled fertilizer or chemical application."""
    if user is None or application is None or not notifications_enabled():
        return

    try:
        preference = get_preference(user)
    except Exception:
        logger.exception('Could not load notification preferences for user %s', getattr(user, 'pk', None))
        return

    if not preference.notify_on_schedule:
        return

    label = application.get_application_type_display()
    subject = 'Hi-Tech Eggplant: {} scheduled for {:%b %d, %Y}'.format(label, application.due_date)
    body = '\n'.join([
        'Hello {},'.format(user.first_name or user.username),
        '',
        '{} is scheduled for {:%B %d, %Y}.'.format(label, application.due_date),
        '',
        'Recommendations:',
        application.recommendations or '(none recorded)',
    ])
    sms = 'HI-TECH EGGPLANT: {} scheduled for {:%b %d}. Check the dashboard for details.'.format(
        label, application.due_date)

    create_web_notification(
        user,
        '{} scheduled'.format(label),
        'Due {:%B %d, %Y}.'.format(application.due_date),
        category='schedule',
        level='info',
        link_url='/fertilizer/',
        preference=preference,
    )
    notify_user_async(user, subject, body, sms_message=sms, preference=preference)
