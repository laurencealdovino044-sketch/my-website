"""Live measurements only; old Firestore records remain available for history."""
import math
from datetime import datetime
from django.utils import timezone

ALIASES = {
    'soil_moisture': ('soil_moisture', 'soil_moisture_percent', 'soilMoisture'),
    'temperature': ('temperature', 'temperature_c', 'temp'),
    'humidity': ('humidity', 'hum'),
    'n': ('n', 'nitrogen'), 'p': ('p', 'phosphorus'), 'k': ('k', 'potassium'),
    'soil_ph': ('soil_ph', 'soil_pH', 'ph'),
    'soil_temperature': ('soil_temperature', 'soil_temp'),
    'soil_ec': ('soil_ec', 'ec', 'electrical_conductivity'),
}


def offline_feed(message='No fresh sensor readings.', timeout=120):
    return {**dict.fromkeys(ALIASES, 0), 'pi_online': False,
            'pi_last_seen': None, 'pi_seconds_since_update': None,
            'pi_offline_after': timeout, 'message': message, 'available_sensors': []}


def numeric(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def read_feed(client, user_id, season_id=None, timeout=120):
    user = client.collection('users').document(user_id)
    collections = [client.collection('sensor_data'), user.collection('sensor_data')]
    if season_id:
        collections.append(user.collection('seasons').document(season_id).collection('sensor_data'))
    candidates = []
    for collection in collections:
        try:
            # Include devices writing sensor_data/latest without a timestamp field.
            documents = collection.get()
            for doc in documents:
                data = doc.to_dict() or {}
                values = {}
                for key, aliases in ALIASES.items():
                    for alias in aliases:
                        value = numeric(data.get(alias))
                        if value is not None:
                            values[key] = value
                            break
                if not values:
                    continue  # Pump commands are not sensor heartbeats.
                stamp = data.get('timestamp', getattr(doc, 'update_time', None))
                if isinstance(stamp, str):
                    try:
                        stamp = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
                    except ValueError:
                        continue
                if not isinstance(stamp, datetime):
                    continue
                if timezone.is_naive(stamp):
                    stamp = timezone.make_aware(stamp)
                candidates.append((stamp, values))
        except Exception:
            continue
    result = offline_feed(timeout=timeout)
    if not candidates:
        return result
    stamp, values = max(candidates, key=lambda item: item[0])
    age = (timezone.now() - stamp).total_seconds()
    result.update(pi_last_seen=stamp.isoformat(), pi_seconds_since_update=max(0, int(age)))
    if -5 <= age <= timeout:
        result.update(values)
        result.update(pi_online=True, message='Live sensor feed.', available_sensors=list(values))
    return result
