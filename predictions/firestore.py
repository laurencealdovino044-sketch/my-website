from __future__ import annotations

import json
import os
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

try:
    import firebase_admin
    from firebase_admin import credentials, firestore
except ImportError:  # pragma: no cover - dependency may be absent outside the venv
    firebase_admin = None
    credentials = None
    firestore = None


_FIRESTORE_CLIENT = None
_LOCAL_SERVICE_ACCOUNT_PATH = Path(__file__).resolve().parent.parent / 'media' / 'firebase-service-account.json'
_ROOT_SERVICE_ACCOUNT_PATH = Path(__file__).resolve().parent.parent / 'firebase-service-account.json'


def _load_service_account_credentials():
    if firebase_admin is None:
        return None

    if _LOCAL_SERVICE_ACCOUNT_PATH.exists():
        with _LOCAL_SERVICE_ACCOUNT_PATH.open('r', encoding='utf-8') as file_handle:
            return credentials.Certificate(json.load(file_handle))

    if _ROOT_SERVICE_ACCOUNT_PATH.exists():
        with _ROOT_SERVICE_ACCOUNT_PATH.open('r', encoding='utf-8') as file_handle:
            return credentials.Certificate(json.load(file_handle))

    raw_credentials = os.environ.get('FIREBASE_CREDENTIALS_JSON')
    if raw_credentials:
        if os.path.exists(raw_credentials):
            with open(raw_credentials, 'r', encoding='utf-8') as file_handle:
                raw_credentials = file_handle.read()
        return credentials.Certificate(json.loads(raw_credentials))

    credentials_path = os.environ.get('GOOGLE_APPLICATION_CREDENTIALS')
    if credentials_path and os.path.exists(credentials_path):
        return credentials.Certificate(credentials_path)

    return None


def get_firestore_client():
    global _FIRESTORE_CLIENT

    if _FIRESTORE_CLIENT is not None:
        return _FIRESTORE_CLIENT

    service_account = _load_service_account_credentials()
    if service_account is None:
        return None

    try:
        firebase_admin.get_app()
    except ValueError:
        firebase_admin.initialize_app(service_account)

    _FIRESTORE_CLIENT = firestore.client()
    return _FIRESTORE_CLIENT


def _serialize(value: Any):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {key: _serialize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_serialize(item) for item in value]
    return value


def _write_document(collection_name: str, document_id: Any, payload: dict[str, Any]):
    client = get_firestore_client()
    if client is None:
        return False

    client.collection(collection_name).document(str(document_id)).set(_serialize(payload), merge=True)
    return True


def sync_user_to_firestore(user):
    if user is None:
        return False

    payload = {
        'id': user.pk,
        'username': user.username,
        'first_name': user.first_name,
        'last_name': user.last_name,
        'email': user.email,
        'is_active': user.is_active,
        'is_staff': user.is_staff,
        'is_superuser': user.is_superuser,
        'date_joined': user.date_joined,
        'last_login': user.last_login,
    }
    return _write_document('users', user.pk, payload)


def sync_activity_history(activity_history):
    if activity_history is None:
        return False

    payload = {
        'id': activity_history.pk,
        'user_id': activity_history.user_id,
        'user_username': getattr(activity_history.user, 'username', None),
        'action': activity_history.action,
        'timestamp': activity_history.timestamp,
        'details': activity_history.details,
    }
    return _write_document('activity_histories', activity_history.pk, payload)


def sync_scheduled_application(application):
    if application is None:
        return False

    payload = {
        'id': application.pk,
        'user_id': application.user_id,
        'user_username': getattr(application.user, 'username', None),
        'application_type': application.application_type,
        'plant_age': application.plant_age,
        'deficiency': application.deficiency,
        'recommendations': application.recommendations,
        'due_date': application.due_date,
        'completed': application.completed,
        'created_at': application.created_at,
    }
    return _write_document('scheduled_applications', application.pk, payload)


def sync_notification_preference(preference):
    if preference is None:
        return False

    payload = {
        'user_id': preference.user_id,
        'user_username': getattr(preference.user, 'username', None),
        'email': getattr(preference.user, 'email', None),
        'phone_number': preference.phone_number,
        'email_enabled': preference.email_enabled,
        'sms_enabled': preference.sms_enabled,
        'web_enabled': preference.web_enabled,
        'notify_on_detection': preference.notify_on_detection,
        'notify_healthy_results': preference.notify_healthy_results,
        'notify_on_schedule': preference.notify_on_schedule,
        'updated_at': preference.updated_at,
    }
    return _write_document('notification_preferences', preference.user_id, payload)
