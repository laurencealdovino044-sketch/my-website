"""Pump requests and spraying schedules in RTDB; sensors stay in Firestore."""
import os
import uuid
from datetime import datetime, timezone

import firebase_admin
from firebase_admin import db

from .firestore import _load_service_account_credentials


def pump_reference(user_id, pump_name=None):
    url = os.environ.get('FIREBASE_DATABASE_URL', '').strip()
    if not url:
        raise RuntimeError('Set FIREBASE_DATABASE_URL to your Firebase Realtime Database URL.')
    try:
        app = firebase_admin.get_app()
    except ValueError:
        credentials = _load_service_account_credentials()
        if credentials is None:
            raise RuntimeError('Firebase service account credentials are missing.')
        app = firebase_admin.initialize_app(credentials)
    path = f'users/{user_id}/pump_control'
    if pump_name is not None:
        if pump_name not in ('water_pump', 'spray_pump'):
            raise ValueError('Unknown pump.')
        path += f'/{pump_name}'
    return db.reference(path, app=app, url=url)


def get_manual_pump_states(user_id):
    data = pump_reference(user_id).get() or {}
    return {name: (data.get(name) or {}).get('enabled') is True
            for name in ('water_pump', 'spray_pump')}


def set_manual_pump(user_id, pump_name, enabled, activated_by):
    if not isinstance(enabled, bool):
        raise ValueError('Pump enabled must be a boolean.')
    pump_reference(user_id, pump_name).update({
        'enabled': enabled,
        'requested_by': activated_by,
        'requested_at': datetime.now(timezone.utc).isoformat(),
    })


def get_spray_schedule(user_id):
    data = pump_reference(user_id, 'spray_pump').child('schedule').get() or {}
    start = data.get('start_datetime')
    return {
        'active': data.get('enabled') is True,
        'duration': data.get('duration', 5),
        'start_datetime': datetime.fromisoformat(start) if start else None,
        'id': data.get('id'),
        'executed_id': data.get('executed_id'),
    }


def save_spray_schedule(user_id, enabled, start_datetime, duration, requested_by):
    duration = int(duration)
    if duration < 1 or duration > 3600:
        raise ValueError('Spraying duration must be between 1 and 3600 seconds.')
    if start_datetime is None or start_datetime.utcoffset() is None:
        raise ValueError('A spraying start time with a timezone is required.')
    pump_reference(user_id, 'spray_pump').child('schedule').set({
        'id': uuid.uuid4().hex,
        'enabled': bool(enabled),
        'start_datetime': start_datetime.isoformat(),
        'duration': duration,
        'requested_by': requested_by,
        'requested_at': datetime.now(timezone.utc).isoformat(),
    })


def disable_spray_schedule(user_id):
    pump_reference(user_id, 'spray_pump').child('schedule').update({'enabled': False})
