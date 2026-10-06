from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, RequestFactory
from django.utils import timezone

from predictions import views


class SprayScheduleViewTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.user = SimpleNamespace(id=7, username='operator', is_authenticated=True)
        self.schedule = {'active': False, 'duration': 12, 'start_datetime': None,
                         'id': None, 'executed_id': None}
        self.saved = self.patch('save_spray_schedule')
        self.disabled = self.patch('disable_spray_schedule')
        self.patch('get_spray_schedule').return_value = self.schedule
        self.patch('next_pending_chemical_application').return_value = None
        self.patch('ActivityHistory')
        self.patch('messages')

    def patch(self, name):
        patcher = patch.object(views, name)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def post(self, view_name, data):
        request = self.factory.post('/', data)
        request.user = self.user
        return getattr(views, view_name)(request)

    def test_both_pages_save_schedule_in_realtime_database(self):
        start = timezone.localtime(timezone.now() + timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M')
        for page in ('automated_watering', 'automated_spraying'):
            response = self.post(page, {'action': 'update_spray_schedule',
                                       'spray_start_datetime': start, 'spray_duration': '20'})
            self.assertEqual(response.status_code, 302)
            args = self.saved.call_args.args
            self.assertEqual(args[:2], ('7', False))
            self.assertEqual(args[2].strftime('%Y-%m-%dT%H:%M'), start)
            self.assertEqual(args[3:], (5, 'operator'))

    def test_turning_off_cancels_schedule_in_database(self):
        self.schedule['active'] = True
        self.post('automated_spraying', {'action': 'toggle_spray_schedule'})
        self.disabled.assert_called_once_with('7')
        self.saved.assert_not_called()

    def test_turning_on_uses_saved_start_time(self):
        start = timezone.now() + timedelta(hours=1)
        self.schedule.update(start_datetime=start, id='saved')
        self.post('automated_spraying', {'action': 'toggle_spray_schedule'})
        self.saved.assert_called_once_with('7', True, start, 5, 'operator')

    def test_chemical_task_arms_database_without_local_pump_thread(self):
        due = timezone.localdate() + timedelta(days=1)
        start = views.arm_automated_spraying(self.user, due, 'treatment')
        self.assertEqual(timezone.localtime(start, views.SPRAY_TIMEZONE).hour, 6)
        self.saved.assert_called_once_with('7', True, start, 5, 'operator')

    def test_reading_schedule_never_rearms_it(self):
        request = self.factory.get('/')
        request.user = self.user
        self.schedule.update(active=True, id='saved', executed_id='saved')
        context = views._spray_schedule_context(request)
        self.assertEqual(context['spraying_schedule'], self.schedule)
        self.saved.assert_not_called()

    def test_database_failure_is_reported_without_local_fallback(self):
        self.saved.side_effect = RuntimeError('offline')
        with self.assertLogs(level='ERROR'):
            response = self.post('automated_spraying', {'action': 'toggle_spray_schedule'})
        self.assertEqual(response.status_code, 302)
        views.messages.error.assert_called_once()
