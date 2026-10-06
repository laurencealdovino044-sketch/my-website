from django.test import TestCase, Client, override_settings
from django.contrib.auth.models import User
from django.core import mail
from django.urls import reverse
from django.utils import timezone
from datetime import datetime, timedelta
from unittest import mock
import time
from predictions.models import GoogleIdentity, Notification, NotificationPreference
from predictions.views import watering_schedule, spraying_schedule, water_pump, spray_pump
import json

class AutomatedWateringTestCase(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(username='testuser', password='testpass')
        self.client.login(username='testuser', password='testpass')

    def test_automated_watering_get(self):
        """Test GET request to automated_watering view"""
        response = self.client.get(reverse('automated_watering'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Watering System')
        self.assertContains(response, 'Manual Watering')
        self.assertContains(response, 'Manual Spraying')

    def test_manual_water_post(self):
        """Test manual water POST request"""
        response = self.client.post(reverse('automated_watering'), {'action': 'manual_water'})
        self.assertEqual(response.status_code, 302)  # Redirect
        self.assertRedirects(response, reverse('automated_watering'))

    def test_manual_spray_post(self):
        """Test manual spray POST request"""
        response = self.client.post(reverse('automated_watering'), {'action': 'manual_spray'})
        self.assertEqual(response.status_code, 302)  # Redirect
        self.assertRedirects(response, reverse('automated_watering'))

    def test_toggle_water_schedule(self):
        """Test toggling water schedule"""
        initial_state = watering_schedule['active']
        response = self.client.post(reverse('automated_watering'), {'action': 'toggle_water_schedule'})
        self.assertEqual(response.status_code, 302)
        self.assertNotEqual(watering_schedule['active'], initial_state)

    def test_water_schedule_defaults_to_6am_daily(self):
        """Automatic watering should run at 6:00 AM every day."""
        watering_schedule['active'] = False
        watering_schedule['start_datetime'] = None

        response = self.client.post(reverse('automated_watering'), {'action': 'toggle_water_schedule'})

        self.assertEqual(response.status_code, 302)
        self.assertTrue(watering_schedule['active'])
        self.assertIsNotNone(watering_schedule['start_datetime'])
        self.assertEqual(timezone.localtime(watering_schedule['start_datetime']).hour, 6)
        self.assertEqual(timezone.localtime(watering_schedule['start_datetime']).minute, 0)

    @mock.patch('predictions.views.save_spray_schedule')
    @mock.patch('predictions.views.get_spray_schedule', return_value={
        'active': False, 'duration': 5, 'start_datetime': None,
        'id': None, 'executed_id': None})
    def test_toggle_spray_schedule(self, get_schedule, save_schedule):
        """Test toggling spray schedule"""
        response = self.client.post(reverse('automated_watering'), {'action': 'toggle_spray_schedule'})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(save_schedule.call_args.args[1])
        self.assertEqual(save_schedule.call_args.args[0], str(self.user.id))

    def test_start_live_scan_allows_drone_rtmp_without_capture_source(self):
        """Drone RTMP streams should not require a local capture source."""
        response = self.client.post(
            reverse('start_live_scan'),
            data=json.dumps({
                'rtmp_url': 'rtmp://192.168.1.10/live',
                'stream_key': 'drone-feed-123',
                'device': 'diji_neo'
            }),
            content_type='application/json'
        )

        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertTrue(data.get('ok'))
        self.assertIn('publish_url', data)

    def test_update_water_schedule(self):
        """Test updating water schedule parameters"""
        start_dt = timezone.localtime(timezone.now() + timedelta(minutes=10)).strftime('%Y-%m-%dT%H:%M')
        response = self.client.post(reverse('automated_watering'), {
            'action': 'update_water_schedule',
            'water_duration': '15',
            'water_start_datetime': start_dt,
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(watering_schedule['duration'], 15)
        self.assertIsNotNone(watering_schedule['start_datetime'])
        self.assertEqual(
            watering_schedule['start_datetime'].strftime('%Y-%m-%dT%H:%M'),
            start_dt
        )

    @mock.patch('predictions.views.save_spray_schedule')
    @mock.patch('predictions.views.get_spray_schedule', return_value={
        'active': False, 'duration': 5, 'start_datetime': None,
        'id': None, 'executed_id': None})
    def test_update_spray_schedule(self, get_schedule, save_schedule):
        """Test updating spray schedule parameters"""
        start_dt = timezone.localtime(timezone.now() + timedelta(minutes=15)).strftime('%Y-%m-%dT%H:%M')
        response = self.client.post(reverse('automated_watering'), {
            'action': 'update_spray_schedule',
            'spray_duration': '10',
            'spray_start_datetime': start_dt,
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(save_schedule.call_args.args[3], 5)
        self.assertEqual(
            save_schedule.call_args.args[2].strftime('%Y-%m-%dT%H:%M'),
            start_dt
        )

    def test_live_predict_with_spraying(self):
        """Test live_predict view with spraying trigger"""
        # Create a minimal valid base64 image
        image_data = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=='
        spraying_schedule['active'] = True
        response = self.client.post(reverse('live_predict'), {'image': image_data})
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertIn('predicted_class', data)
        self.assertIn('confidence', data)
        self.assertIn('recommendation', data)

    def test_unauthenticated_access(self):
        """Test that unauthenticated users are redirected"""
        self.client.logout()
        response = self.client.get(reverse('automated_watering'))
        self.assertEqual(response.status_code, 302)  # Redirect to login

class PumpTestCase(TestCase):
    def test_pump_operations(self):
        """Test pump on/off operations"""
        # These should not raise exceptions
        water_pump.on()
        water_pump.off()
        spray_pump.on()
        spray_pump.off()
        # If we get here without exceptions, the test passes
        self.assertTrue(True)


class PredictViewTestCase(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(username='predict-user', password='testpass')
        self.client.login(username='predict-user', password='testpass')

    def test_predict_page_is_available(self):
        response = self.client.get(reverse('predict'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Disease Detection')


class NotificationHelperTestCase(TestCase):
    """The plumbing behind e-mail/SMS alerts: number cleanup, gating, delivery."""

    def setUp(self):
        self.user = User.objects.create_user(
            username='notify-user', password='testpass', email='farmer@example.com')

    def test_local_numbers_are_converted_to_e164(self):
        from predictions.notifications import normalize_phone_number

        self.assertEqual(normalize_phone_number('09171234567'), '+639171234567')
        self.assertEqual(normalize_phone_number('0917 123 4567'), '+639171234567')
        self.assertEqual(normalize_phone_number('+639171234567'), '+639171234567')
        self.assertEqual(normalize_phone_number('00639171234567'), '+639171234567')

    def test_unusable_numbers_are_rejected(self):
        from predictions.notifications import normalize_phone_number

        self.assertEqual(normalize_phone_number(''), '')
        self.assertEqual(normalize_phone_number('12345'), '')
        self.assertEqual(normalize_phone_number('not a phone'), '')

    @override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
    def test_notify_user_uses_only_the_enabled_channels(self):
        from predictions.notifications import get_preference, notify_user

        preference = get_preference(self.user)
        preference.email_enabled = True
        preference.sms_enabled = False
        preference.phone_number = '+639171234567'
        preference.save()

        results = notify_user(self.user, 'Subject', 'Body')

        self.assertIsNone(results['sms'])
        self.assertTrue(results['email'][0])
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ['farmer@example.com'])

    @override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend', SMS_PROVIDER='console')
    def test_notify_user_sends_sms_when_switched_on(self):
        from predictions.notifications import get_preference, notify_user

        preference = get_preference(self.user)
        preference.sms_enabled = True
        preference.phone_number = '+639171234567'
        preference.save()

        results = notify_user(self.user, 'Subject', 'Body', sms_message='Short text')

        self.assertTrue(results['sms'][0])

    def test_healthy_results_are_not_announced_by_default(self):
        from predictions import notifications

        with mock.patch.object(notifications, 'notify_user_async') as dispatch:
            notifications.notify_disease_detection(self.user, 'Healthy Eggplant Leaf', 99.0)
            dispatch.assert_not_called()

            notifications.notify_disease_detection(self.user, 'Aphids', 91.5, {'recommendation': 'Spray.'})
            self.assertEqual(dispatch.call_count, 1)

    def test_detections_are_silent_when_the_user_opted_out(self):
        from predictions import notifications

        preference = notifications.get_preference(self.user)
        preference.notify_on_detection = False
        preference.save()

        with mock.patch.object(notifications, 'notify_user_async') as dispatch:
            notifications.notify_disease_detection(self.user, 'Aphids', 91.5)
            dispatch.assert_not_called()

    def test_alert_text_names_the_disease_and_the_treatment(self):
        from predictions.notifications import build_detection_alert

        info = {'about': 'Sap-sucking insects.', 'medicine': ['Confidor'], 'recommendation': 'Spray early morning.'}
        subject, body, sms = build_detection_alert(self.user, 'Aphids', 97.3, info, 'live')

        self.assertIn('Aphids', subject)
        self.assertIn('97.3%', body)
        self.assertIn('Confidor', body)
        self.assertIn('Spray early morning.', sms)
        self.assertLessEqual(len(sms), 300)


class NotificationSettingsViewTestCase(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(
            username='settings-user', password='testpass', email='farmer@example.com')
        self.client.login(username='settings-user', password='testpass')

    def test_page_renders_both_channels(self):
        response = self.client.get(reverse('notification_settings'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Notifications')
        self.assertContains(response, 'Mobile number')

    def test_saving_stores_preferences_and_normalizes_the_number(self):
        response = self.client.post(reverse('notification_settings'), {
            'email': 'new@example.com',
            'phone_number': '0917 123 4567',
            'email_enabled': 'on',
            'sms_enabled': 'on',
            'notify_on_detection': 'on',
        })
        self.assertRedirects(response, reverse('notification_settings'))

        self.user.refresh_from_db()
        preference = NotificationPreference.objects.get(user=self.user)
        self.assertEqual(self.user.email, 'new@example.com')
        self.assertEqual(preference.phone_number, '+639171234567')
        self.assertTrue(preference.sms_enabled)
        self.assertTrue(preference.notify_on_detection)
        self.assertFalse(preference.notify_on_schedule)  # unchecked box means off

    def test_sms_cannot_be_enabled_without_a_number(self):
        response = self.client.post(reverse('notification_settings'), {
            'email': 'farmer@example.com',
            'phone_number': '',
            'sms_enabled': 'on',
        })
        self.assertRedirects(response, reverse('notification_settings'))
        self.assertFalse(NotificationPreference.objects.get(user=self.user).sms_enabled)

    def test_invalid_number_is_refused(self):
        response = self.client.post(reverse('notification_settings'), {
            'email': 'farmer@example.com',
            'phone_number': '12345',
            'sms_enabled': 'on',
        })
        self.assertRedirects(response, reverse('notification_settings'))
        self.assertEqual(NotificationPreference.objects.get(user=self.user).phone_number, '')

    @override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
    def test_test_alert_is_sent_on_the_enabled_channels(self):
        preference, _ = NotificationPreference.objects.get_or_create(user=self.user)
        preference.email_enabled = True
        preference.save()

        response = self.client.post(reverse('send_test_notification'))

        self.assertRedirects(response, reverse('notification_settings'))
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('test', mail.outbox[0].subject.lower())

    def test_test_alert_requires_a_channel(self):
        preference, _ = NotificationPreference.objects.get_or_create(user=self.user)
        preference.email_enabled = False
        preference.sms_enabled = False
        preference.save()

        self.client.post(reverse('send_test_notification'))
        self.assertEqual(len(mail.outbox), 0)

    def test_login_is_required(self):
        self.client.logout()
        response = self.client.get(reverse('notification_settings'))
        self.assertEqual(response.status_code, 302)


class WebNotificationTestCase(TestCase):
    """The in-app channel: the bell feed, unread counts, and mark-as-read."""

    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(
            username='bell-user', password='testpass', email='farmer@example.com')
        self.client.login(username='bell-user', password='testpass')

    def _make(self, title='Aphids detected', is_read=False, level='danger'):
        return Notification.objects.create(
            user=self.user, title=title, message='92% confidence.', level=level,
            category='detection', is_read=is_read)

    def test_detection_files_a_bell_entry(self):
        from predictions import notifications

        with mock.patch.object(notifications, 'notify_user_async'):
            notifications.notify_disease_detection(
                self.user, 'Aphids', 92.0, {'recommendation': 'Spray.'}, source='live')

        notification = Notification.objects.get(user=self.user)
        self.assertIn('Aphids', notification.title)
        self.assertIn('92.0%', notification.message)
        self.assertEqual(notification.category, 'detection')
        self.assertEqual(notification.level, 'danger')
        self.assertFalse(notification.is_read)

    def test_healthy_detection_is_filed_as_success_when_opted_in(self):
        from predictions import notifications

        preference = notifications.get_preference(self.user)
        preference.notify_healthy_results = True
        preference.save()

        with mock.patch.object(notifications, 'notify_user_async'):
            notifications.notify_disease_detection(self.user, 'Healthy Eggplant Leaf', 99.0)

        self.assertEqual(Notification.objects.get(user=self.user).level, 'success')

    def test_nothing_is_filed_when_the_bell_is_switched_off(self):
        from predictions import notifications

        preference = notifications.get_preference(self.user)
        preference.web_enabled = False
        preference.save()

        with mock.patch.object(notifications, 'notify_user_async'):
            notifications.notify_disease_detection(self.user, 'Aphids', 92.0)

        self.assertEqual(Notification.objects.count(), 0)

    def test_feed_returns_unread_count_and_items(self):
        self._make('Older', is_read=True)
        self._make('Newer')

        response = self.client.get(reverse('notification_feed'))
        data = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(data['ok'])
        self.assertEqual(data['unread_count'], 1)
        self.assertEqual(len(data['notifications']), 2)
        self.assertEqual(data['notifications'][0]['title'], 'Newer')  # newest first
        self.assertIn('age', data['notifications'][0])

    def test_feed_reports_only_what_arrived_since_the_last_poll(self):
        first = self._make('First')
        second = self._make('Second')

        data = self.client.get(reverse('notification_feed'), {'since': first.id}).json()

        self.assertEqual([item['id'] for item in data['new']], [second.id])
        self.assertEqual(data['latest_id'], second.id)

    def test_feed_never_leaks_another_users_notifications(self):
        other = User.objects.create_user(username='other-farmer', password='testpass')
        Notification.objects.create(user=other, title='Not yours', message='')
        self._make('Mine')

        data = self.client.get(reverse('notification_feed')).json()

        self.assertEqual([item['title'] for item in data['notifications']], ['Mine'])
        self.assertEqual(data['unread_count'], 1)

    def test_marking_one_read_leaves_the_others_alone(self):
        first = self._make('First')
        self._make('Second')

        response = self.client.post(
            reverse('mark_notifications_read'), {'id': first.id},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest')

        self.assertEqual(response.json()['unread_count'], 1)
        first.refresh_from_db()
        self.assertTrue(first.is_read)
        self.assertIsNotNone(first.read_at)

    def test_marking_all_read_clears_the_badge(self):
        self._make('First')
        self._make('Second')

        response = self.client.post(reverse('mark_notifications_read'))

        self.assertRedirects(response, reverse('notification_inbox'))
        self.assertEqual(Notification.objects.filter(user=self.user, is_read=False).count(), 0)

    def test_a_user_cannot_mark_someone_elses_notification_read(self):
        other = User.objects.create_user(username='other-farmer2', password='testpass')
        theirs = Notification.objects.create(user=other, title='Not yours', message='')

        self.client.post(reverse('mark_notifications_read'), {'id': theirs.id},
                         HTTP_X_REQUESTED_WITH='XMLHttpRequest')

        theirs.refresh_from_db()
        self.assertFalse(theirs.is_read)

    def test_inbox_lists_notifications(self):
        self._make('Aphids detected')

        response = self.client.get(reverse('notification_inbox'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Aphids detected')
        self.assertContains(response, '1 unread')

    def test_inbox_has_an_empty_state(self):
        response = self.client.get(reverse('notification_inbox'))
        self.assertContains(response, 'Nothing here yet')

    def test_bell_is_rendered_in_the_shell_for_signed_in_users(self):
        response = self.client.get(reverse('notification_inbox'))
        self.assertContains(response, 'id="notifBellBtn"')
        self.assertContains(response, 'id="notifPanel"')

    def test_feed_requires_login(self):
        self.client.logout()
        response = self.client.get(reverse('notification_feed'))
        self.assertEqual(response.status_code, 302)

    @override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
    def test_test_alert_also_lands_in_the_bell(self):
        self.client.post(reverse('send_test_notification'))

        notification = Notification.objects.get(user=self.user)
        self.assertEqual(notification.category, 'test')
        self.assertIn('test', notification.title.lower())


class SmsDeliveryTestCase(TestCase):
    """What actually goes over the wire to the SMS gateway, and when.

    The gateways are exercised through a mocked requests.post: these assert the
    request Twilio/Semaphore would receive, which is the part that cannot be
    checked without spending real credits.
    """

    def setUp(self):
        self.user = User.objects.create_user(
            username='sms-user', password='testpass', email='farmer@example.com')
        # The signal already made a row for this user; adjust it rather than adding a second.
        preference = NotificationPreference.objects.get(user=self.user)
        preference.sms_enabled = True
        preference.phone_number = '+639171234567'
        preference.save()

    @staticmethod
    def _response(status_code=201, payload=None, text=''):
        response = mock.Mock()
        response.status_code = status_code
        response.text = text
        response.json.return_value = payload if payload is not None else {}
        return response

    @override_settings(
        SMS_PROVIDER='twilio',
        TWILIO_ACCOUNT_SID='AC0000000000',
        TWILIO_AUTH_TOKEN='secret-token',
        TWILIO_FROM_NUMBER='+15550001111',
    )
    def test_twilio_request_carries_the_number_and_the_text(self):
        from predictions import notifications

        with mock.patch.object(notifications.requests, 'post', return_value=self._response()) as post:
            sent, detail = notifications.send_sms_notification('09171234567', 'Aphids detected')

        self.assertTrue(sent, detail)
        url = post.call_args.args[0]
        data = post.call_args.kwargs['data']
        self.assertIn('AC0000000000/Messages.json', url)
        self.assertEqual(post.call_args.kwargs['auth'], ('AC0000000000', 'secret-token'))
        self.assertEqual(data['To'], '+639171234567')      # normalized from 0917...
        self.assertEqual(data['From'], '+15550001111')
        self.assertEqual(data['Body'], 'Aphids detected')

    @override_settings(SMS_PROVIDER='semaphore', SEMAPHORE_API_KEY='key-123', SEMAPHORE_SENDER_NAME='HITECHEGG')
    def test_semaphore_request_carries_the_api_key_and_sender(self):
        from predictions import notifications

        with mock.patch.object(notifications.requests, 'post', return_value=self._response(200)) as post:
            sent, detail = notifications.send_sms_notification('09171234567', 'Aphids detected')

        self.assertTrue(sent, detail)
        data = post.call_args.kwargs['data']
        self.assertEqual(data['apikey'], 'key-123')
        self.assertEqual(data['number'], '+639171234567')
        self.assertEqual(data['sendername'], 'HITECHEGG')

    @override_settings(
        SMS_PROVIDER='twilio',
        TWILIO_ACCOUNT_SID='AC0000000000',
        TWILIO_AUTH_TOKEN='secret-token',
        TWILIO_FROM_NUMBER='+15550001111',
    )
    def test_a_rejected_message_is_reported_not_raised(self):
        from predictions import notifications

        rejection = self._response(401, {'message': 'Authenticate'}, 'Authenticate')
        with mock.patch.object(notifications.requests, 'post', return_value=rejection):
            sent, detail = notifications.send_sms_notification('09171234567', 'Aphids detected')

        self.assertFalse(sent)
        self.assertIn('401', detail)
        self.assertIn('Authenticate', detail)

    @override_settings(
        SMS_PROVIDER='twilio',
        TWILIO_ACCOUNT_SID='AC0000000000',
        TWILIO_AUTH_TOKEN='secret-token',
        TWILIO_FROM_NUMBER='+15550001111',
        EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
    )
    def test_a_detection_reaches_the_gateway_by_itself(self):
        """The bell entry and the text are the same event: filing one sends the other."""
        from predictions import notifications

        with mock.patch.object(notifications.requests, 'post', return_value=self._response()) as post:
            notifications.notify_disease_detection(
                self.user, 'Aphids', 92.0, {'recommendation': 'Spray early morning.'}, source='live')

            # Delivery runs on a daemon thread; wait for it rather than sleeping blindly.
            deadline = time.time() + 10
            while not post.call_count and time.time() < deadline:
                time.sleep(0.05)

        self.assertEqual(post.call_count, 1)
        body = post.call_args.kwargs['data']['Body']
        self.assertIn('Aphids', body)
        self.assertIn('92.0%', body)
        self.assertLessEqual(len(body), 300)

        # ... and the same event is in the bell, so nothing depends on the gateway working.
        self.assertEqual(Notification.objects.filter(user=self.user).count(), 1)

    @override_settings(SMS_PROVIDER='twilio', TWILIO_ACCOUNT_SID='', TWILIO_AUTH_TOKEN='', TWILIO_FROM_NUMBER='')
    def test_an_unconfigured_gateway_says_so_instead_of_pretending(self):
        from predictions import notifications

        sent, detail = notifications.send_sms_notification('09171234567', 'Aphids detected')

        self.assertFalse(sent)
        self.assertIn('not configured', detail)


def google_claims(**overrides):
    """A verified Google ID token payload, as google-auth would hand it back."""
    claims = {
        'iss': 'https://accounts.google.com',
        'sub': '1234567890',
        'email': 'farmer@gmail.com',
        'email_verified': True,
        'given_name': 'Juaquin',
        'family_name': 'Tabernero',
        'picture': 'https://lh3.googleusercontent.com/a/photo',
    }
    claims.update(overrides)
    return claims


@override_settings(GOOGLE_OAUTH_CLIENT_ID='test-client-id.apps.googleusercontent.com')
class GoogleSignInTestCase(TestCase):
    """Sign in with Google: token verification, account matching, and the button."""

    def setUp(self):
        self.client = Client()
        self.url = reverse('google_login')

    def _post(self, claims=None, credential='fake-id-token'):
        """Post a credential, with Google's verification stubbed to return `claims`."""
        from predictions import google_auth

        if claims is None:
            claims = google_claims()
        with mock.patch.object(google_auth.google_id_token, 'verify_oauth2_token', return_value=claims):
            return self.client.post(self.url, {'credential': credential})

    def test_first_sign_in_creates_an_account_and_logs_in(self):
        response = self._post()

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['ok'])
        self.assertTrue(data['created'])
        self.assertEqual(data['redirect'], reverse('home'))

        user = User.objects.get(email='farmer@gmail.com')
        self.assertEqual(user.username, 'farmer')
        self.assertEqual(user.first_name, 'Juaquin')
        self.assertFalse(user.has_usable_password())
        self.assertEqual(GoogleIdentity.objects.get(user=user).sub, '1234567890')
        self.assertEqual(int(self.client.session['_auth_user_id']), user.pk)

    def test_second_sign_in_reuses_the_same_account(self):
        self._post()
        self.client.logout()
        response = self._post()

        self.assertFalse(response.json()['created'])
        self.assertEqual(User.objects.count(), 1)
        self.assertEqual(GoogleIdentity.objects.count(), 1)

    def test_a_changed_google_address_follows_the_same_account(self):
        self._post()
        self.client.logout()
        self._post(google_claims(email='new-address@gmail.com'))

        self.assertEqual(User.objects.count(), 1)
        self.assertEqual(User.objects.get().email, 'new-address@gmail.com')

    def test_an_existing_password_account_is_linked_by_verified_email(self):
        existing = User.objects.create_user(
            username='juaquin', password='testpass', email='farmer@gmail.com')

        response = self._post()

        self.assertFalse(response.json()['created'])
        self.assertEqual(User.objects.count(), 1)
        self.assertEqual(GoogleIdentity.objects.get().user_id, existing.pk)
        existing.refresh_from_db()
        # Linking must not disturb the password they already sign in with.
        self.assertTrue(existing.check_password('testpass'))

    def test_an_unverified_google_address_is_refused(self):
        User.objects.create_user(username='juaquin', password='testpass', email='farmer@gmail.com')

        response = self._post(google_claims(email_verified=False))

        self.assertEqual(response.status_code, 400)
        self.assertIn('not verified', response.json()['error'])
        self.assertNotIn('_auth_user_id', self.client.session)
        self.assertEqual(GoogleIdentity.objects.count(), 0)

    def test_a_username_clash_gets_a_free_username(self):
        User.objects.create_user(username='farmer', password='testpass', email='someone@else.com')

        self._post()

        self.assertTrue(User.objects.filter(username='farmer1').exists())

    def test_a_disabled_account_cannot_sign_in(self):
        user = User.objects.create_user(
            username='juaquin', password='testpass', email='farmer@gmail.com')
        user.is_active = False
        user.save()

        response = self._post()

        self.assertEqual(response.status_code, 400)
        self.assertIn('disabled', response.json()['error'])
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_a_forged_token_is_rejected(self):
        from predictions import google_auth

        with mock.patch.object(google_auth.google_id_token, 'verify_oauth2_token',
                               side_effect=ValueError('Token has wrong audience')):
            response = self.client.post(self.url, {'credential': 'forged'})

        self.assertEqual(response.status_code, 400)
        self.assertNotIn('_auth_user_id', self.client.session)
        self.assertEqual(User.objects.count(), 0)

    def test_a_token_from_another_issuer_is_rejected(self):
        response = self._post(google_claims(iss='https://evil.example.com'))

        self.assertEqual(response.status_code, 400)
        self.assertEqual(User.objects.count(), 0)

    def test_a_missing_credential_is_rejected(self):
        response = self.client.post(self.url, {})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(User.objects.count(), 0)

    def test_get_is_not_allowed(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)

    def test_the_button_appears_on_both_auth_pages(self):
        for name in ('login', 'signup'):
            response = self.client.get(reverse(name))
            self.assertContains(response, 'googleAuthSlot', msg_prefix=name)
            self.assertContains(response, 'test-client-id.apps.googleusercontent.com', msg_prefix=name)
            self.assertContains(response, 'accounts.google.com/gsi/client', msg_prefix=name)

    @override_settings(GOOGLE_OAUTH_CLIENT_ID='')
    def test_without_a_client_id_the_pages_hide_the_button(self):
        for name in ('login', 'signup'):
            response = self.client.get(reverse(name))
            self.assertEqual(response.status_code, 200, name)
            self.assertNotContains(response, 'googleAuthSlot', msg_prefix=name)

    @override_settings(GOOGLE_OAUTH_CLIENT_ID='')
    def test_without_a_client_id_the_endpoint_refuses(self):
        response = self.client.post(self.url, {'credential': 'anything'})

        self.assertEqual(response.status_code, 400)
        self.assertIn('not configured', response.json()['error'])

    @override_settings(GOOGLE_OAUTH_CLIENT_ID='', DEBUG=True)
    def test_development_pages_say_why_the_button_is_missing(self):
        for name in ('login', 'signup'):
            response = self.client.get(reverse(name))
            self.assertContains(response, 'not set up yet', msg_prefix=name)
            self.assertContains(response, 'GOOGLE_OAUTH_CLIENT_ID', msg_prefix=name)

    @override_settings(GOOGLE_OAUTH_CLIENT_ID='', DEBUG=False)
    def test_production_pages_never_mention_the_setup(self):
        """A live site must not advertise its own missing configuration."""
        for name in ('login', 'signup'):
            response = self.client.get(reverse(name))
            self.assertEqual(response.status_code, 200, name)
            self.assertNotContains(response, 'not set up yet', msg_prefix=name)
            self.assertNotContains(response, 'GOOGLE_OAUTH_CLIENT_ID', msg_prefix=name)

    def test_a_google_account_gets_notification_settings_like_any_other(self):
        self._post()

        user = User.objects.get(email='farmer@gmail.com')
        self.assertTrue(NotificationPreference.objects.filter(user=user).exists())
