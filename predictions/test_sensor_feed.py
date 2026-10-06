from datetime import timedelta
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock
from django.utils import timezone
from .sensor_feed import ALIASES, read_feed


class SensorFeedTests(TestCase):
    def feed(self, data=None, age=0, failure=False, user_data=None):
        client = MagicMock()
        shared = client.collection.return_value
        def document(payload, seconds):
            stamp = timezone.now() - timedelta(seconds=seconds)
            return SimpleNamespace(to_dict=lambda: payload, update_time=stamp)
        shared.get.return_value = [] if data is None else [document(data, age)]
        if failure:
            shared.get.side_effect = RuntimeError('unavailable')
        user = shared.document.return_value.collection.return_value
        user.get.return_value = [] if user_data is None else [document(user_data, 0)]
        return read_feed(client, '1')

    def assert_offline(self, result):
        self.assertFalse(result['pi_online'])
        self.assertTrue(all(result[key] == 0 for key in ALIASES))

    def test_missing_and_unreachable(self):
        self.assert_offline(self.feed())
        self.assert_offline(self.feed(failure=True))

    def test_stale_readings_zero_all_sensors(self):
        self.assert_offline(self.feed(dict.fromkeys(ALIASES, 35), age=121))

    def test_fresh_aliases_and_missing_values(self):
        data = self.feed({'temp': 29, 'nitrogen': '42', 'ph': 6.5})
        self.assertTrue(data['pi_online'])
        self.assertEqual(data['temperature'], 29)
        self.assertEqual(data['n'], 42)
        self.assertEqual(data['soil_ph'], 6.5)
        self.assertEqual(data['humidity'], 0)

    def test_pump_writes_are_not_readings(self):
        self.assert_offline(self.feed({'water_pump_status': 'active'}))

    def test_all_seven_soil_readings_reach_home_feed(self):
        values = {'soil_moisture': 42, 'soil_temperature': 25.5,
                  'soil_ec': 200, 'soil_ph': 6.5, 'n': 50, 'p': 60, 'k': 70}
        result = self.feed(user_data=values)
        self.assertTrue(result['pi_online'])
        for key, value in values.items():
            self.assertEqual(result[key], value)
            self.assertIn(key, result['available_sensors'])

    def test_soil_temperature_and_conductivity_aliases(self):
        result = self.feed({'soil_temp': '25.5', 'electrical_conductivity': '200'})
        self.assertEqual(result['soil_temperature'], 25.5)
        self.assertEqual(result['soil_ec'], 200)

    def test_device_timestamp_wins_over_recent_update(self):
        self.assert_offline(self.feed({'temperature': 30, 'timestamp': (timezone.now() - timedelta(minutes=5)).isoformat()}))

    def test_invalid_measurements_and_timestamps(self):
        for data in ({'temperature': 'NaN'}, {'humidity': True}, {'n': 'bad'},
                     {'temperature': 30, 'timestamp': 'invalid'},
                     {'temperature': 30, 'timestamp': (timezone.now() + timedelta(days=1)).isoformat()}):
            self.assert_offline(self.feed(data))

    def test_fresh_user_reading_overrides_stale_shared_reading(self):
        result = self.feed({'temperature': 10}, age=300, user_data={'temperature': 28})
        self.assertTrue(result['pi_online'])
        self.assertEqual(result['temperature'], 28)
