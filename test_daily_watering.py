import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch
from daily_watering import DailyWatering, WeatherRain


class DailyWateringTests(unittest.TestCase):
    def setUp(self):
        self.saved = {}
        self.reference = Mock()
        self.reference.get.side_effect = lambda: dict(self.saved)
        def transaction(callback):
            self.saved = callback(dict(self.saved))
            return dict(self.saved)
        self.reference.transaction.side_effect = transaction
        self.now = datetime(2026, 10, 5, 22, tzinfo=timezone.utc)
        self.start = Mock(return_value=True)

    def test_dry_day_starts_once_even_after_restart_and_runs_next_day(self):
        for day in (self.now, self.now, self.now + timedelta(days=1)):
            DailyWatering(self.reference, lambda: False).tick(self.start, now=day)
        self.assertEqual(self.start.call_count, 2)

    def test_rain_and_unknown_weather_skip_entire_day(self):
        for rain in (True, None):
            self.saved = {}
            DailyWatering(self.reference, lambda: rain).tick(self.start, now=self.now)
            DailyWatering(self.reference, lambda: False).tick(self.start, now=self.now)
            self.assertTrue(self.saved['result'].startswith('skipped_rain'))
        self.start.assert_not_called()

    def test_other_times_and_busy_hardware_do_not_start(self):
        scheduler = DailyWatering(self.reference, lambda: False)
        scheduler.tick(self.start, now=self.now - timedelta(seconds=1))
        scheduler.tick(self.start, now=self.now + timedelta(minutes=1))
        scheduler.tick(self.start, busy=True, now=self.now)
        self.reference.transaction.assert_not_called()

    def test_losing_claim_does_not_start(self):
        self.reference.transaction.side_effect = lambda callback: {'token': 'other'}
        DailyWatering(self.reference, lambda: False).tick(self.start, now=self.now)
        self.start.assert_not_called()

    @patch('daily_watering.json.load')
    @patch('daily_watering.urlopen')
    def test_weather_conditions_and_stale_data(self, request, load):
        with patch.dict('os.environ', {'OPENWEATHER_API_KEY': 'test'}):
            for code, expected in ((500, True), (300, True), (200, True), (800, False), (804, False)):
                load.return_value = {'dt': datetime.now(timezone.utc).timestamp(), 'weather': [{'id': code}]}
                self.assertIs(WeatherRain()(), expected)
            load.return_value['dt'] = 0
            self.assertIsNone(WeatherRain()())
            request.side_effect = TimeoutError
            self.assertIsNone(WeatherRain()())


if __name__ == '__main__':
    unittest.main()
