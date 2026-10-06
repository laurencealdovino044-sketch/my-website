import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from predictions import realtime
from raspi_realtime_pump_control import PumpController


class RealtimePumpTests(unittest.TestCase):
    @patch.object(realtime, 'pump_reference')
    def test_on_and_off_are_explicit_booleans(self, reference):
        for enabled in (True, False):
            realtime.set_manual_pump('7', 'water_pump', enabled, 'operator')
            reference.assert_called_with('7', 'water_pump')
            payload = reference.return_value.update.call_args.args[0]
            self.assertIs(payload['enabled'], enabled)
            self.assertEqual(payload['requested_by'], 'operator')

    @patch.object(realtime, 'pump_reference')
    def test_state_is_read_from_database(self, reference):
        reference.return_value.get.return_value = {'water_pump': {'enabled': True}}
        self.assertEqual(realtime.get_manual_pump_states('7'),
                         {'water_pump': True, 'spray_pump': False})

    @patch.dict('os.environ', {'FIREBASE_DATABASE_URL': ''})
    def test_missing_url_fails_before_any_write(self):
        with self.assertRaisesRegex(RuntimeError, 'FIREBASE_DATABASE_URL'):
            realtime.set_manual_pump('7', 'water_pump', True, 'operator')

    def test_listener_applies_on_and_off_and_reports_actual_state(self):
        control, status, water, spray = Mock(), Mock(), Mock(), Mock()
        controller = PumpController(control, status, {'water_pump': water, 'spray_pump': spray})
        control.get.return_value = {'water_pump': {'enabled': True}, 'spray_pump': {'enabled': False}}
        controller.apply_event(Mock())
        water.on.assert_called_once()
        spray.off.assert_called_once()
        control.get.return_value = {'water_pump': {'enabled': False}, 'spray_pump': {'enabled': True}}
        controller.apply_event(Mock())
        water.off.assert_called_once()
        spray.on.assert_called_once()
        self.assertEqual(status.child.call_count, 4)


class SprayScheduleTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc)
        self.schedule = {'id': 'job1', 'enabled': True,
                         'start_datetime': self.now.isoformat(), 'duration': 10}
        self.control = Mock()
        self.control.get.side_effect = lambda: {'spray_pump': {'enabled': False, 'schedule': deepcopy(self.schedule)}}

        def transaction(callback):
            self.schedule = callback(deepcopy(self.schedule))
            return deepcopy(self.schedule)

        self.control.child.return_value.transaction.side_effect = transaction
        self.spray = Mock(value=False)
        self.spray.on.side_effect = lambda: setattr(self.spray, 'value', True)
        self.spray.off.side_effect = lambda: setattr(self.spray, 'value', False)
        self.controller = PumpController(self.control, Mock(), {'spray_pump': self.spray})
        self.controller.apply_event(Mock())
        self.timer_patch = patch('raspi_realtime_pump_control.threading.Timer')
        self.timer = self.timer_patch.start()
        self.addCleanup(self.timer_patch.stop)

    def test_due_schedule_starts_and_local_timer_stops_it(self):
        self.controller.tick(self.now)
        self.assertTrue(self.spray.value)
        self.timer.return_value.start.assert_called_once()
        self.assertGreater(self.timer.call_args.args[0], 9)
        self.controller.finish_schedule('job1')
        self.assertFalse(self.spray.value)
        self.assertEqual(self.schedule['executed_id'], 'job1')

    def test_reconnect_and_second_listener_cannot_replay_claimed_schedule(self):
        self.controller.tick(self.now)
        self.controller.finish_schedule('job1')
        other_pump = Mock(value=False)
        other = PumpController(self.control, Mock(), {'spray_pump': other_pump})
        # Stale snapshot held by a second process before the first claimed the job.
        other.requests = {'spray_pump': {'schedule': {k: v for k, v in self.schedule.items()
                                                     if k not in ('executed_id', 'execution_token')}}}
        other.tick(self.now)
        other_pump.on.assert_not_called()
        self.controller.apply_event(Mock())
        self.controller.tick(self.now)
        self.assertFalse(self.spray.value)

    def test_future_and_expired_schedules_do_not_start(self):
        self.controller.tick(self.now - timedelta(seconds=1))
        self.controller.tick(self.now + timedelta(seconds=11))
        self.control.child.return_value.transaction.assert_not_called()
        self.assertFalse(self.spray.value)

    def test_disable_stops_running_schedule(self):
        self.controller.tick(self.now)
        self.schedule['enabled'] = False
        self.controller.apply_event(Mock())
        self.assertFalse(self.spray.value)
        self.timer.return_value.cancel.assert_called_once()

    @patch.object(realtime, 'pump_reference')
    def test_schedule_saves_timezone_and_duration_and_can_be_read_after_restart(self, reference):
        realtime.save_spray_schedule('7', True, self.now, 12, 'operator')
        payload = reference.return_value.child.return_value.set.call_args.args[0]
        reference.return_value.child.return_value.get.return_value = payload
        schedule = realtime.get_spray_schedule('7')
        self.assertTrue(schedule['active'])
        self.assertEqual(schedule['start_datetime'], self.now)
        self.assertEqual(schedule['duration'], 12)

    @patch.object(realtime, 'pump_reference')
    def test_invalid_duration_does_not_write(self, reference):
        for duration in (0, -1, 3601):
            with self.assertRaises(ValueError):
                realtime.save_spray_schedule('7', True, self.now, duration, 'operator')
        reference.assert_not_called()

    def test_listener_failure_turns_both_outputs_off(self):
        control, water, spray = Mock(), Mock(), Mock()
        control.get.side_effect = RuntimeError('offline')
        controller = PumpController(control, Mock(), {'water_pump': water, 'spray_pump': spray})
        with self.assertLogs(level='ERROR'):
            controller.apply_event(Mock())
        water.off.assert_called_once()
        spray.off.assert_called_once()
