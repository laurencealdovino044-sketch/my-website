"""Permanent 06:00 Manila watering, with persistent daily claims and rain checks."""
import logging
import os
import json
import time
import uuid
from urllib.parse import urlencode
from urllib.request import urlopen
from datetime import datetime, timedelta, timezone

MANILA = timezone(timedelta(hours=8))


class WeatherRain:
    """OpenWeather current conditions, cached briefly to limit API requests."""
    def __init__(self):
        self.checked_at = None
        self.value = None

    def __call__(self):
        if self.checked_at is not None and time.monotonic() - self.checked_at < 30:
            return self.value
        self.value = None
        try:
            key = os.environ.get('OPENWEATHER_API_KEY')
            city = os.environ.get('WATERING_WEATHER_CITY', 'Naujan,Oriental Mindoro,PH')
            if not key or not city:
                raise ValueError('Set OPENWEATHER_API_KEY and WATERING_WEATHER_CITY on the Pi')
            query = urlencode({'q': city, 'appid': key, 'units': 'metric'})
            with urlopen('https://api.openweathermap.org/data/2.5/weather?' + query, timeout=10) as response:
                data = json.load(response)
            conditions = data.get('weather')
            age = time.time() - float(data['dt'])
            if not conditions or not -300 <= age <= 1800:
                raise ValueError('Missing or stale weather data')
            codes = [int(item['id']) for item in conditions]
            if any(not (200 <= code < 600 or 600 <= code < 800 or 800 <= code <= 804) for code in codes):
                raise ValueError('Unknown weather condition')
            self.value = any(200 <= code < 600 for code in codes) or any(
                float(amount) > 0 for amount in (data.get('rain') or {}).values())
        except Exception:
            logging.warning('Rain check unavailable; automatic watering will be skipped')
        self.checked_at = time.monotonic()
        return self.value


class DailyWatering:
    def __init__(self, reference, raining):
        self.reference = reference
        self.raining = raining

    def tick(self, start, busy=False, now=None):
        local = (now or datetime.now(timezone.utc)).astimezone(MANILA)
        if local.hour != 6 or local.minute != 0 or busy:
            return
        day = local.date().isoformat()
        token = uuid.uuid4().hex
        try:
            if (self.reference.get() or {}).get('date') == day:
                return
            # Unknown rain state is never treated as dry weather.
            rain = self.raining()
            result = 'skipped_rain' if rain is True else 'skipped_rain_unknown'
            if rain is False:
                result = 'claimed'

            def claim(current):
                current = current or {}
                if current.get('date') == day:
                    return current
                return {'date': day, 'time': '06:00', 'timezone': 'Asia/Manila',
                        'token': token, 'result': result,
                        'updated_at': {'.sv': 'timestamp'}}

            claimed = self.reference.transaction(claim)
            if not claimed or claimed.get('token') != token:
                return
            if result == 'claimed':
                # Recheck after the network claim, immediately before activation.
                rain = self.raining()
                if rain is False:
                    result = 'started' if start() else 'skipped_busy'
                else:
                    result = 'skipped_rain' if rain is True else 'skipped_rain_unknown'
                def finish(current):
                    if current and current.get('token') == token:
                        return {**current, 'result': result}
                    return current
                self.reference.transaction(finish)
            logging.info('Daily watering %s: %s', day, result)
        except Exception:
            logging.exception('Daily watering could not run')
