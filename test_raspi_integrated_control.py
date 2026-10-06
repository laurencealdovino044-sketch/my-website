import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import raspi_integrated_control as controller
_MISSING = object()


class IntegratedControllerTests(unittest.TestCase):
    def setUp(self):
        controller.stop_event.clear()
        self.patch('current_operation', 'idle')
        self.patch('pump_is_on', False)
        self.patch('worker_thread', None)
        self.patch('worker_pump', None)
        self.patch('worker_schedule_id', None)
        self.patch('worker_manual_request', False)
        self.patch('handled_requests', {})
        self.patch('web_commands', {})

    def patch(self, name, value=_MISSING):
        patcher = patch.object(controller, name, Mock() if value is _MISSING else value, create=True)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def test_live_sensors_use_dashboard_firestore_path_and_field_names(self):
        database = self.patch('firestore_db')
        soil = {'moisture': 42, 'temperature': 25, 'ec': 200,
                'ph': 6.5, 'n': 50, 'p': 60, 'k': 70}
        controller.update_sensor_database(soil, 29, 75, False)
        database.collection.assert_called_once_with('users')
        user = database.collection.return_value.document
        user.assert_called_once_with(controller.USER_ID)
        collection = user.return_value.collection
        collection.assert_called_once_with('sensor_data')
        collection.return_value.document.assert_called_once_with('latest')
        payload = collection.return_value.document.return_value.set.call_args.args[0]
        self.assertEqual((payload['temperature'], payload['humidity'], payload['n']), (29, 75, 50))
        self.assertIn('timestamp', payload)

    def test_manual_buttons_start_the_correct_hardware_cycle(self):
        start = self.patch('start_web_operation')
        for name in ('water_pump', 'spray_pump'):
            controller.web_commands = {name: {'enabled': True, 'requested_at': name}}
            controller.process_web_controls()
            start.assert_called_with(name, request=controller.web_commands[name])

    def test_manual_off_stops_active_cycle(self):
        self.patch('worker_thread', Mock(is_alive=Mock(return_value=True)))
        self.patch('worker_pump', 'spray_pump')
        self.patch('worker_manual_request', True)
        stop = self.patch('emergency_stop')
        controller.web_commands = {'spray_pump': {'enabled': False}}
        controller.process_web_controls()
        stop.assert_called_once()

    def test_shared_pump_cannot_run_both_operations_at_once(self):
        start = self.patch('start_web_operation')
        status = self.patch('update_status')
        controller.web_commands = {name: {'enabled': True} for name in ('water_pump', 'spray_pump')}
        controller.process_web_controls()
        start.assert_not_called()
        self.assertIn('Shared pump', status.call_args.kwargs['error'])

    def test_schedule_is_claimed_once_and_uses_original_chemical_cycle(self):
        now = datetime(2026, 10, 6, 0, 0, tzinfo=timezone.utc)
        schedule = {'id': 'job', 'enabled': True, 'start_datetime': now.isoformat(), 'duration': 5}
        reference = self.patch('web_control_ref')
        reference.child.return_value.transaction.side_effect = lambda callback: callback(schedule)
        start = self.patch('start_web_operation')
        controller.web_commands = {'spray_pump': {'schedule': schedule}}
        controller.process_web_controls(now)
        name = start.call_args.args[0]
        claimed = start.call_args.kwargs['schedule']
        self.assertEqual(name, 'spray_pump')
        self.assertEqual(claimed['executed_id'], 'job')
        controller.web_commands = {'spray_pump': {'schedule': claimed}}
        controller.process_web_controls(now)
        start.assert_called_once()
        start.reset_mock()
        controller.web_commands = {'spray_pump': {'schedule': schedule}}
        controller.process_web_controls(now + timedelta(minutes=3))
        start.assert_not_called()

    def test_disabling_schedule_stops_active_spray(self):
        self.patch('worker_thread', Mock(is_alive=Mock(return_value=True)))
        self.patch('worker_pump', 'spray_pump')
        self.patch('worker_schedule_id', 'job')
        stop = self.patch('emergency_stop')
        controller.web_commands = {'spray_pump': {'schedule': {'id': 'job', 'enabled': False}}}
        controller.process_web_controls()
        stop.assert_called_once()

    def test_chemical_cycle_preserves_four_endpoint_passes(self):
        self.patch('update_status')
        self.patch('lcd_message')
        self.patch('water_pump_on')
        off = self.patch('water_pump_off')
        self.patch('motors_forward')
        self.patch('motors_backward')
        self.patch('motors_stop')
        self.patch('wait_until_black')
        self.patch('operation_ref')
        database = self.patch('firestore_db')
        white = self.patch('is_white')
        white.side_effect = [False, True, True, True, True]
        with patch.object(controller.time, 'sleep'):
            controller.chemical_cycle()
        payload = database.collection.return_value.add.call_args.args[0]
        self.assertEqual(payload['passes'], 4)
        self.assertEqual(payload['round_trips'], 2)
        self.assertEqual(controller.current_operation, 'idle')
        self.assertTrue(off.called)

    def test_old_completion_does_not_clear_new_manual_click(self):
        reference = self.patch('web_control_ref')
        current = {'enabled': True, 'requested_at': 'new'}
        reference.child.return_value.transaction.side_effect = lambda callback: callback(current)
        controller.acknowledge_manual('water_pump', {'requested_at': 'old'})
        self.assertTrue(current['enabled'])


if __name__ == '__main__':
    unittest.main()
