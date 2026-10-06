import os
from zoneinfo import ZoneInfo
from django.contrib.auth import login, authenticate
from django.contrib.auth.forms import UserCreationForm
from django.shortcuts import render, redirect
from django.contrib.auth import logout
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.conf import settings
from django.core.files.storage import FileSystemStorage
from django.contrib.auth.forms import UserChangeForm
from ultralytics import YOLO
import base64
from io import BytesIO
from django.http import JsonResponse
import threading
import time
import urllib.parse
import io
from .realtime import (get_manual_pump_states, set_manual_pump, get_spray_schedule,
                       save_spray_schedule, disable_spray_schedule)
from .models import ActivityHistory, Notification, NotificationPreference, ScheduledApplication
from .google_auth import (
    GoogleAuthError,
    get_or_create_user_from_google,
    google_login_is_configured,
    verify_google_token,
)
from .notifications import (
    email_is_configured,
    normalize_phone_number,
    notify_disease_detection,
    notify_scheduled_application,
    notify_user,
    sms_is_configured,
    sms_provider,
)
from django.utils import timezone
from datetime import datetime, date, timedelta
from django.views.decorators.http import require_POST
from django.urls import reverse
from django.http import JsonResponse, HttpResponse
import requests
import secrets

import json
import subprocess
import shlex
import shutil
import logging

from streams.models import Stream, StreamLog
from streams.services import kick_publisher, sync_stream, sync_stream_states

# live stream process tracking: map user_id -> subprocess.Popen
live_streams = {}


def resolve_ffmpeg_binary():
    """Locate an FFmpeg binary available in this environment."""
    candidates = []
    ffmpeg_path = shutil.which('ffmpeg')
    if ffmpeg_path:
        candidates.append(ffmpeg_path)

    if os.name == 'nt':
        candidates.extend([
            r'C:\Users\Laurence\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-9.0-full_build\bin\ffmpeg.exe',
            r'C:\Program Files\FFmpeg\bin\ffmpeg.exe',
            r'C:\Program Files (x86)\FFmpeg\bin\ffmpeg.exe',
        ])
    else:
        candidates.extend(['/usr/bin/ffmpeg', '/usr/local/bin/ffmpeg'])

    for candidate in candidates:
        if candidate and os.path.exists(candidate):
            return candidate
    return 'ffmpeg'

# GPIO setup for Raspberry Pi
try:
    from gpiozero import LED, MCP3008, Button
    import Adafruit_DHT
    water_pump = LED(17)  # GPIO 17 for watering
    spray_pump = LED(18)  # GPIO 18 for spraying
    soil_sensor = MCP3008(channel=0)  # ADC channel 0 for soil moisture
    n_sensor = MCP3008(channel=1)      # ADC channel 1 for Nitrogen
    p_sensor = MCP3008(channel=2)      # ADC channel 2 for Phosphorus
    k_sensor = MCP3008(channel=3)      # ADC channel 3 for Potassium
    DHT_SENSOR = Adafruit_DHT.DHT11
    DHT_PIN = 4  # GPIO 4 for DHT11
    is_raspberry_pi = True
except ImportError:
    # Mock for non-Raspberry Pi environments
    class MockLED:
        def on(self): pass
        def off(self): pass
    class MockMCP3008:
        def __init__(self, channel=0):
            self.channel = channel
        @property
        def value(self):
            if self.channel == 0:
                return 0.5  # Mock soil moisture value
            elif self.channel == 1:
                return 0.3 # Mock N value
            elif self.channel == 2:
                return 0.4 # Mock P value
            elif self.channel == 3:
                return 0.6 # Mock K value
            return 0.5
    class MockDHT:
        @staticmethod
        def read_retry(sensor, pin):
            return 25.0, 60.0  # Mock temperature and humidity
    water_pump = MockLED()
    spray_pump = MockLED()
    soil_sensor = MockMCP3008(channel=0)
    n_sensor = MockMCP3008(channel=1)
    p_sensor = MockMCP3008(channel=2)
    k_sensor = MockMCP3008(channel=3)
    DHT_SENSOR = MockDHT
    DHT_PIN = 4
    is_raspberry_pi = False

# Scheduling dictionaries
watering_schedule = {
    'active': False,
    'duration': 5,
    'start_datetime': None,
}

spraying_schedule = {
    'active': False,
    'duration': 5,
    'start_datetime': None,
}

manual_watering = {
    'active': False,
}

manual_spraying = {
    'active': False,
}

# Background threads
watering_thread = None
spraying_thread = None

def _parse_schedule_datetime(datetime_str):
    if not datetime_str:
        return None
    try:
        parsed = datetime.strptime(datetime_str, '%Y-%m-%dT%H:%M')
        if timezone.is_naive(parsed):
            parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
        return parsed
    except Exception:
        return None


def _format_schedule_datetime(dt):
    if not dt:
        dt = timezone.now()
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_current_timezone())
    return timezone.localtime(dt).strftime('%Y-%m-%dT%H:%M')


def _next_daily_6am_datetime(reference_time=None):
    ref = timezone.localtime(reference_time or timezone.now())
    target = ref.replace(hour=6, minute=0, second=0, microsecond=0)
    if ref >= target:
        target = target + timedelta(days=1)
    if timezone.is_naive(target):
        target = timezone.make_aware(target, timezone.get_current_timezone())
    return target


def _load_prediction_model():
    configured_model_path = os.environ.get('PREDICTION_MODEL_PATH')
    if configured_model_path:
        model_path = configured_model_path
        if not os.path.isabs(model_path):
            model_path = os.path.join(settings.BASE_DIR, model_path)
    else:
        model_path = os.path.join(settings.BASE_DIR, 'static/models/best.pt')

    return YOLO(model_path)


model = _load_prediction_model()


from .firestore import get_firestore_client
from firebase_admin import firestore


GROWTH_STAGES = [
    {'key': 'seed', 'label': 'Seed', 'emoji': '\U0001F331', 'day_start': 1, 'day_end': 7,
     'description': 'The seed is planted and begins to grow.',
     'steps': [
         'Select good-quality seeds.',
         'Prepare the seedbed or seedling tray.',
         'Plant the seeds in the soil.',
         'Water the seeds regularly.',
         'Place them in an area with enough sunlight.',
     ]},
    {'key': 'germination', 'label': 'Germination', 'emoji': '\U0001F33F', 'day_start': 7, 'day_end': 14,
     'description': 'The roots and small leaves of the plant begin to emerge.',
     'steps': [
         'Check if the seeds have started to grow.',
         'Keep the soil moist.',
         'Remove weeds if necessary.',
         'Check for pests and diseases.',
         'Record the germination date.',
     ]},
    {'key': 'seedling', 'label': 'Seedling', 'emoji': '\U0001F331', 'day_start': 14, 'day_end': 35,
     'description': 'The young plant grows bigger and develops more leaves.',
     'steps': [
         'Water the seedlings regularly.',
         'Provide enough sunlight.',
         'Monitor leaf growth.',
         'Apply fertilizer when necessary.',
         'Check for pests and diseases.',
         'Prepare healthy seedlings for transplanting.',
     ]},
    {'key': 'vegetative', 'label': 'Vegetative Growth', 'emoji': '\U0001F333', 'day_start': 35, 'day_end': 60,
     'description': 'The stem, roots, and leaves continue to grow.',
     'steps': [
         'Transplant the seedlings to the growing area.',
         'Water the plants regularly.',
         'Apply fertilizer when needed.',
         'Remove weeds.',
         'Monitor plant height and number of leaves.',
         'Check for diseases and pests.',
         'Record plant observations.',
     ]},
    {'key': 'flowering', 'label': 'Flowering', 'emoji': '\U0001F338', 'day_start': 60, 'day_end': 80,
     'description': 'The eggplant begins to flower.',
     'steps': [
         'Monitor the appearance of flowers.',
         'Continue regular watering.',
         'Apply fertilizer when necessary.',
         'Check flowers for pests and diseases.',
         'Record the flowering date.',
         'Monitor the number of flowers.',
     ]},
    {'key': 'fruiting', 'label': 'Fruiting', 'emoji': '\U0001F346', 'day_start': 75, 'day_end': 100,
     'description': 'The flowers turn into fruit and the eggplant gradually grows bigger.',
     'steps': [
         'Monitor fruit development.',
         'Water the plants regularly.',
         'Apply fertilizer when necessary.',
         'Check fruits for diseases and pests.',
         'Monitor fruit size and quantity.',
         'Remove damaged fruits when necessary.',
     ]},
    {'key': 'harvesting', 'label': 'Harvesting', 'emoji': '\U0001F9FA', 'day_start': 90, 'day_end': 120,
     'description': 'The eggplant can be harvested once it has reached the right size and good quality.',
     'steps': [
         'Check if the eggplants are mature.',
         'Harvest fruits with the proper size and quality.',
         'Weigh the harvested eggplants.',
         'Record the quantity and weight.',
         'Record the harvest date.',
         'Update the total production record.',
     ]},
]


# Inaraw-araw is the short-season native pick: it fruits earlier than the imported
# varieties and is then picked a little every day, so its harvest window is opened
# early and left wide instead of being treated as a one-off event at the end.
INARAW_ARAW_GROWTH_STAGES = [
    {'key': 'seed', 'label': 'Seed', 'emoji': '\U0001F331', 'day_start': 1, 'day_end': 6,
     'description': 'The seed is planted and begins to grow.',
     'steps': [
         'Select good-quality Inaraw-araw seeds from a healthy previous harvest.',
         'Prepare the seedbed or seedling tray.',
         'Plant the seeds in the soil.',
         'Water the seeds lightly every day.',
         'Place them in an area with enough sunlight.',
     ]},
    {'key': 'germination', 'label': 'Germination', 'emoji': '\U0001F33F', 'day_start': 6, 'day_end': 12,
     'description': 'The roots and small leaves of the plant begin to emerge.',
     'steps': [
         'Check if the seeds have started to grow.',
         'Keep the soil moist but not waterlogged.',
         'Remove weeds if necessary.',
         'Check for pests and diseases.',
         'Record the germination date.',
     ]},
    {'key': 'seedling', 'label': 'Seedling', 'emoji': '\U0001F331', 'day_start': 12, 'day_end': 30,
     'description': 'The young plant grows bigger and develops more leaves.',
     'steps': [
         'Water the seedlings regularly.',
         'Provide enough sunlight.',
         'Monitor leaf growth.',
         'Apply fertilizer when necessary.',
         'Check for pests and diseases.',
         'Prepare healthy seedlings for transplanting.',
     ]},
    {'key': 'vegetative', 'label': 'Vegetative Growth', 'emoji': '\U0001F333', 'day_start': 30, 'day_end': 50,
     'description': 'The stem, roots, and leaves continue to grow.',
     'steps': [
         'Transplant the seedlings to the growing area.',
         'Water the plants regularly.',
         'Apply fertilizer when needed.',
         'Remove weeds.',
         'Monitor plant height and number of leaves.',
         'Check for diseases and pests.',
         'Record plant observations.',
     ]},
    {'key': 'flowering', 'label': 'Flowering', 'emoji': '\U0001F338', 'day_start': 50, 'day_end': 65,
     'description': 'The eggplant begins to flower. Inaraw-araw keeps flowering while it fruits, so new flowers appear all season.',
     'steps': [
         'Monitor the appearance of flowers.',
         'Water the plants consistently — irregular watering drops the flowers.',
         'Apply fertilizer that supports flowering.',
         'Check for pests that attack the flowers.',
         'Record the number of flowers.',
     ]},
    {'key': 'fruiting', 'label': 'Fruiting', 'emoji': '\U0001F346', 'day_start': 60, 'day_end': 80,
     'description': 'The first fruits set. New fruits keep coming behind them, so the plant carries fruit at every size at once.',
     'steps': [
         'Monitor the growth of the fruits.',
         'Water the plants regularly.',
         'Apply fertilizer to support fruit development.',
         'Check for fruit borers and other pests.',
         'Remove damaged fruits when necessary.',
     ]},
    {'key': 'harvesting', 'label': 'Harvesting', 'emoji': '\U0001F9FA', 'day_start': 70, 'day_end': 130,
     'description': 'Harvest a little every day. Picking the mature fruits daily keeps the plant producing longer.',
     'steps': [
         'Go around the plants every day and pick the fruits that are already the right size.',
         'Harvest in the morning while the fruits are still firm.',
         'Do not leave overripe fruits on the plant — they stop new fruits from setting.',
         'Weigh each day’s harvest.',
         'Record the quantity and weight daily.',
         'Keep fertilizing and watering between pickings to extend the harvest.',
     ]},
]


# Growth guides that differ from the default timeline, keyed by the variety name
# stored on the season (the same strings offered in the Start a New Season form).
VARIETY_GROWTH_STAGES = {
    'Inaraw-araw': INARAW_ARAW_GROWTH_STAGES,
}


def get_growth_stages(variety):
    """The stage guide for a variety, falling back to the default eggplant timeline."""
    if not variety:
        return GROWTH_STAGES
    return VARIETY_GROWTH_STAGES.get(str(variety).strip(), GROWTH_STAGES)


DISEASE_INFO = {
    'Aphids': {
        'icon': 'bug',
        'about': 'Aphids are sap-sucking insects that weaken eggplant plants and spread viruses.',
        'medicine_label': 'Recommended Medicine',
        'medicine': ['Confidor (Imidacloprid)', 'Actara (Thiamethoxam)', 'Neem Oil Spray'],
        'recommendation': 'Spray early morning or late afternoon. Remove heavily infected leaves.',
    },
    'Caterpillars': {
        'icon': 'worm',
        'about': 'Caterpillars chew large holes in leaves and can bore into eggplant fruits.',
        'medicine_label': 'Recommended Medicine',
        'medicine': ['Bacillus thuringiensis (Bt) Spray', 'Cymbush (Cypermethrin)', 'Neem Oil Spray'],
        'recommendation': 'Handpick the caterpillars you can see, then spray in the late afternoon.',
    },
    'Cercospora Leaf Spot': {
        'icon': 'virus',
        'about': 'Cercospora leaf spot is a fungal disease causing circular brown spots on leaves.',
        'medicine_label': 'Recommended Medicine',
        'medicine': ['Daconil (Chlorothalonil)', 'Dithane M-45 (Mancozeb)', 'Antracol Fungicide'],
        'recommendation': 'Remove infected leaves and avoid overhead watering.',
    },
    'Defect Eggplant': {
        'icon': 'triangle-exclamation',
        'about': 'Defective eggplant fruits may result from pests, diseases, or nutrient deficiency.',
        'medicine_label': 'Recommended Action',
        'medicine': ['Remove damaged fruits', 'Improve fertilization', 'Monitor pests regularly'],
        'recommendation': 'Maintain proper irrigation and balanced nutrients.',
    },
    'Epilachna Beetle Infestation': {
        'icon': 'bug',
        'about': 'Epilachna (hadda) beetles and their grubs scrape leaf tissue, leaving skeletonised leaves.',
        'medicine_label': 'Recommended Medicine',
        'medicine': ['Cymbush (Cypermethrin)', 'Karate (Lambda-Cyhalothrin)', 'Neem Oil Spray'],
        'recommendation': 'Collect the beetles and egg clusters by hand, then spray the underside of leaves.',
    },
    'Flea Beetles': {
        'icon': 'bug',
        'about': 'Flea beetles create tiny holes on leaves and weaken the plant.',
        'medicine_label': 'Recommended Medicine',
        'medicine': ['Cymbush (Cypermethrin)', 'Karate (Lambda-Cyhalothrin)', 'Neem Oil Spray'],
        'recommendation': 'Use yellow sticky traps and spray insecticide if infestation increases.',
    },
    'Fungal Blight': {
        'icon': 'cloud-rain',
        'about': 'Fungal blight spreads brown, water-soaked patches that dry out and kill the leaf.',
        'medicine_label': 'Recommended Medicine',
        'medicine': ['Dithane M-45 (Mancozeb)', 'Daconil (Chlorothalonil)', 'Ridomil Gold'],
        'recommendation': 'Remove blighted leaves, avoid overhead watering, and improve airflow between plants.',
    },
    'Healthy Eggplant Leaf': {
        'icon': 'leaf',
        'about': 'Your eggplant leaf is healthy and free from visible diseases.',
        'medicine_label': 'Preventive Medicine',
        'medicine': ['Neem Oil Spray', 'Dithane M-45 preventive spray'],
        'recommendation': 'Continue proper watering, fertilization, and regular monitoring.',
    },
    'Insect Pest Disease': {
        'icon': 'bug',
        'about': 'Insect pest damage weakens plants and may spread diseases.',
        'medicine_label': 'Recommended Medicine',
        'medicine': ['Cymbush (Cypermethrin)', 'CYPEX 50 EC', 'Confidor (Imidacloprid)'],
        'recommendation': 'Remove heavily damaged leaves and apply insecticide properly.',
    },
    'Leaf Spot Disease': {
        'icon': 'virus',
        'about': 'Leaf spot disease causes dark lesions due to fungal infection.',
        'medicine_label': 'Recommended Medicine',
        'medicine': ['Daconil Fungicide', 'Mancozeb Blue 80WP', 'Chlorothalonil Fungicide'],
        'recommendation': 'Improve airflow and avoid excessive moisture.',
    },
    'Mosaic Virus Disease': {
        'icon': 'virus-covid',
        'about': 'Mosaic virus causes yellow-green mosaic patterns and leaf distortion.',
        'medicine_label': 'Recommended Medicine',
        'medicine': ['Confidor (Imidacloprid)', 'Actara (Thiamethoxam)'],
        'recommendation': 'Remove infected plants immediately to stop virus spread.',
    },
    'Small Leaf Disease': {
        'icon': 'leaf',
        'about': 'The model identifies leaves with an unusually small or stunted appearance. A visual detection does not establish the underlying cause.',
        'medicine_label': 'Recommended Action',
        'medicine': ['Inspect affected plants and compare new growth',
                     'Check soil nutrients and growing conditions',
                     'Ask a local agricultural specialist to confirm the cause'],
        'recommendation': 'Confirm the cause before choosing a chemical treatment. Monitor affected plants and record changes in new leaves.',
    },
    'Phytophora Blight': {
        'icon': 'cloud-rain',
        'about': 'Phytophthora blight causes stem rot and plant wilting.',
        'medicine_label': 'Recommended Medicine',
        'medicine': ['Ridomil Gold', 'Metalaxyl Fungicide', 'Copper Hydroxide'],
        'recommendation': 'Improve soil drainage and avoid waterlogging.',
    },
    'Powdery Mildew': {
        'icon': 'snowflake',
        'about': 'Powdery mildew appears as white powder-like spots on leaves.',
        'medicine_label': 'Recommended Medicine',
        'medicine': ['Sulfur Fungicide', 'Score Fungicide', 'Potassium Bicarbonate Spray'],
        'recommendation': 'Increase airflow and avoid overcrowding plants.',
    },
    'Thrips Infestation': {
        'icon': 'bug',
        'about': 'Thrips rasp the leaf surface, leaving silvery streaks and curled, stunted new growth.',
        'medicine_label': 'Recommended Medicine',
        'medicine': ['Confidor (Imidacloprid)', 'Actara (Thiamethoxam)', 'Spinosad Spray'],
        'recommendation': 'Hang blue sticky traps and spray the growing tips where thrips feed.',
    },
    'White Mold Disease': {
        'icon': 'cloud',
        'about': 'White mold disease produces white fungal growth on stems and fruits.',
        'medicine_label': 'Recommended Medicine',
        'medicine': ['Kocide (Copper Hydroxide)', 'Pencozeb (Mancozeb)', 'Copper Oxychloride'],
        'recommendation': 'Remove infected plant parts and reduce humidity.',
    },
    'Wilt Disease': {
        'icon': 'seedling',
        'about': 'Wilt disease causes drooping and drying due to soil pathogens.',
        'medicine_label': 'Recommended Medicine',
        'medicine': ['Copper Oxychloride', 'Kocide Copper Fungicide', 'Ridomil Gold'],
        'recommendation': 'Improve drainage and practice crop rotation.',
    },
}

# Classes verified from static/models/best.pt, in model order.
# Keep older guide entries for historical detections, but only offer current model
# diseases in the checklist and chemical application's disease choices.
MODEL_CLASS_NAMES = (
    'Healthy Leaf', 'Insect Pest Disease', 'Leaf Spot Disease',
    'Mosaic Virus Disease', 'Small Leaf Disease', 'White Mold Disease', 'Wilt Disease',
)
DISEASE_GUIDE_ORDER = list(MODEL_CLASS_NAMES[1:])

# Which predicted classes are insect pests rather than pathogens, so season analytics can
# report "types of insect that came this season" separately from fungal/viral disease.
INSECT_PEST_CLASSES = {
    'Aphids',
    'Caterpillars',
    'Epilachna Beetle Infestation',
    'Flea Beetles',
    'Insect Pest Disease',
    'Thrips Infestation',
}

# The trained model emits compact labels ('EggplantMosaicVirus'); the rest of the app keys
# everything — DISEASE_INFO, season records, notifications, templates — off the display names
# below, so every prediction is translated once, right where it leaves the model.
MODEL_CLASS_ALIASES = {
    'Caterpillars': 'Caterpillars',
    'EggplantMosaicVirus': 'Mosaic Virus Disease',
    'EpilachnaBeetleInfestation': 'Epilachna Beetle Infestation',
    'FungalBlight': 'Fungal Blight',
    'Healthy': 'Healthy Eggplant Leaf',
    'Healthy Leaf': 'Healthy Eggplant Leaf',
    'ThripsInfestation': 'Thrips Infestation',
}


def normalize_predicted_class(raw_class):
    """Map a raw model label onto the display name the rest of the app uses."""
    return MODEL_CLASS_ALIASES.get(raw_class, raw_class)

# Losses the farmer records by hand — the model can't see a seed that never sprouted or a
# seedling a bird carried off, so these are logged from the Variety Guide page.
SEASON_INCIDENT_TYPES = [
    {'key': 'seeds_not_germinated', 'label': "Seeds didn't grow", 'icon': 'seedling'},
    {'key': 'eaten_by_birds', 'label': 'Eaten by birds', 'icon': 'crow'},
    {'key': 'eaten_by_animals', 'label': 'Eaten by other animals', 'icon': 'paw'},
    {'key': 'weather_damage', 'label': 'Weather damage', 'icon': 'cloud-showers-heavy'},
    {'key': 'other', 'label': 'Other loss', 'icon': 'circle-exclamation'},
]
SEASON_INCIDENT_LABELS = {item['key']: item for item in SEASON_INCIDENT_TYPES}

# The Pi is considered online only while it keeps writing sensor documents to Firestore.
# If the newest reading is older than this, treat the device as offline.
RASPBERRY_PI_OFFLINE_AFTER_SECONDS = getattr(settings, 'RASPBERRY_PI_OFFLINE_AFTER_SECONDS', 120)

# Only the Pi can measure these. The web app also writes into the same sensor_data collection
# when logging pump actions (see update_latest_sensor_pump_status), and those documents carry
# just a timestamp and a *_pump_status field. Requiring a real measurement keeps a button press
# in the dashboard from making an absent device look online.
PI_SENSOR_READING_KEYS = frozenset({
    'soil_moisture', 'soil_moisture_percent', 'soilMoisture',
    'temperature', 'temp', 'temperature_c',
    'humidity', 'hum',
    'n', 'p', 'k',
    'nitrogen', 'phosphorus', 'potassium',
    'soil_ph', 'ph', 'soil_pH',
})


def has_sensor_readings(document_data):
    """True when a Firestore document carries at least one real measurement from the device."""
    if not document_data:
        return False
    return any(key in document_data and document_data[key] is not None for key in PI_SENSOR_READING_KEYS)


def parse_sensor_timestamp(value):
    """Best-effort parse of the many timestamp shapes Firestore documents arrive in."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        for fmt in ('%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S.%f'):
            try:
                return datetime.strptime(value, fmt)
            except Exception:
                continue
        try:
            return datetime.fromisoformat(value)
        except Exception:
            return datetime.min
    if hasattr(value, 'to_datetime'):
        try:
            return value.to_datetime()
        except Exception:
            return datetime.min
    return datetime.min


def document_written_at(doc):
    """When this document was last written.

    Prefers the device's own 'timestamp' field, because Firestore's update_time also moves when
    this app merges pump status into an existing document. Falls back to update_time for devices
    that write no timestamp at all (the shared sensor_data/latest document works this way).
    """
    parsed = parse_sensor_timestamp((doc.to_dict() or {}).get('timestamp'))
    if parsed and parsed != datetime.min:
        return parsed
    return getattr(doc, 'update_time', None)


def document_write_sort_key(doc):
    """Comparable write time for sorting, tolerant of naive/aware/missing timestamps."""
    dt = document_written_at(doc)
    if not isinstance(dt, datetime):
        return float('-inf')
    if timezone.is_naive(dt):
        try:
            dt = timezone.make_aware(dt, timezone.get_current_timezone())
        except Exception:
            return float('-inf')
    try:
        return dt.timestamp()
    except Exception:
        return float('-inf')


def choose_latest_doc(documents):
    """Newest document by write time, whatever it contains."""
    if not documents:
        return None
    return max(documents, key=document_write_sort_key)


def newer_doc(first, second):
    """Whichever of two documents was written most recently."""
    candidates = [doc for doc in (first, second) if doc is not None]
    return choose_latest_doc(candidates)


def choose_latest_reading_doc(documents):
    """Newest document holding an actual measurement, ignoring app-written pump stubs."""
    readings = [doc for doc in (documents or []) if has_sensor_readings(doc.to_dict() or {})]
    return choose_latest_doc(readings)


def seconds_since_timestamp(value):
    """Age in seconds of a Firestore timestamp, or None when it is missing/unparseable."""
    if value is None or value == datetime.min:
        return None
    if not isinstance(value, datetime):
        return None
    dt = value
    if timezone.is_naive(dt):
        try:
            dt = timezone.make_aware(dt, timezone.get_current_timezone())
        except Exception:
            return None
    return max((timezone.now() - dt).total_seconds(), 0)


# The plant-age choices on the fertilizer/chemical forms. Defined here rather than in the
# template so the season's real age can be matched against them and pre-selected.
PLANT_AGE_OPTIONS = [
    (2, '2 weeks (Seedling)'),
    (4, '4 weeks (Early growth)'),
    (6, '6 weeks (Vegetative)'),
    (8, '8 weeks (Pre-flowering)'),
    (10, '10 weeks (Flowering)'),
    (12, '12 weeks (Fruiting)'),
    (16, '16+ weeks (Mature)'),
]


def season_plant_age_weeks(season):
    """The season's age in whole weeks, taken from the same day count the tracker shows."""
    if not season:
        return None
    try:
        current_day = int(season.get('current_day', 1))
    except (TypeError, ValueError):
        return None
    return max((current_day + 6) // 7, 1)


def plant_age_option_for(weeks):
    """The closest form option at or below the plant's real age, so advice is never too advanced."""
    if weeks is None:
        return None
    eligible = [value for value, _ in PLANT_AGE_OPTIONS if value <= weeks]
    return max(eligible) if eligible else PLANT_AGE_OPTIONS[0][0]


def get_latest_disease_detection(season):
    """The most recent disease the Predict tool recorded on this season, or None.

    Reads the same detected_diseases list the Variety Guide checks items off from, so the
    chemical form and the guide can never disagree about what was last found.
    """
    if not season:
        return None

    detections = [d for d in (season.get('detected_diseases') or []) if d.get('name')]
    if not detections:
        return None

    def _detected_at(entry):
        # Sort on epoch seconds, not the datetimes themselves: entries written before and
        # after USE_TZ was switched on would otherwise be naive/aware and refuse to compare.
        parsed = parse_sensor_timestamp(entry.get('last_detected') or entry.get('first_detected'))
        if not isinstance(parsed, datetime) or parsed == datetime.min:
            return float('-inf')
        if timezone.is_naive(parsed):
            try:
                parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
            except Exception:
                return float('-inf')
        try:
            return parsed.timestamp()
        except Exception:
            return float('-inf')

    return max(detections, key=_detected_at)


# Soil NPK is reported by the RS485 probe in ppm (mg/kg), the same units the dashboard tiles
# and the analytics charts display. These are the cut-offs the fertilizer recommendations and
# the deficiency detection both read from, so the page can never disagree with itself.
NPK_LOW_PPM = 50
NPK_HIGH_PPM = 150

NPK_NUTRIENT_LABELS = {'n': 'Nitrogen', 'p': 'Phosphorus', 'k': 'Potassium'}
NPK_DEFICIENCY_NAMES = {'n': 'nitrogen', 'p': 'phosphorus', 'k': 'potassium'}


def get_latest_npk_reading(user_id, season_id=None):
    """The newest real N/P/K measurement the device published, or None if it never reported.

    Reads the same documents the dashboard tiles and analytics charts use (season
    subcollection, user-level collection, and the shared top-level one the Pi writes to),
    via the collector defined further down this module. Callers that already hold the active
    season pass its id so this does not re-query it. Never call this from get_active_season
    itself — the fallback lookup below would recurse.
    """
    client = get_firestore_client()
    if client is None:
        return None

    if season_id is None:
        active_season = get_active_season(user_id)
        season_id = active_season.get('id') if active_season else None

    try:
        docs = _collect_analytics_docs(client, user_id, season_id)
    except Exception as lookup_error:
        print(f"[get_latest_npk_reading] sensor lookup failed: {lookup_error}")
        return None

    # Only documents that actually carry one of n/p/k count — a reading with just temperature
    # would otherwise win on recency and report the soil as having no nutrients at all.
    npk_docs = []
    for doc in docs or []:
        values = _analytics_reading(doc.to_dict() or {})
        if any(values.get(key) is not None for key in ('n', 'p', 'k')):
            npk_docs.append((doc, values))

    if not npk_docs:
        return None

    doc, values = max(npk_docs, key=lambda pair: document_write_sort_key(pair[0]))
    written_at = document_written_at(doc)
    age_seconds = seconds_since_timestamp(written_at)

    return {
        'n': values.get('n'),
        'p': values.get('p'),
        'k': values.get('k'),
        'measured_at': written_at if isinstance(written_at, datetime) else None,
        'age_seconds': age_seconds,
        'is_fresh': age_seconds is not None and age_seconds <= RASPBERRY_PI_OFFLINE_AFTER_SECONDS,
    }


def _format_npk_reading(reading):
    """'N 42 ppm, P 88 ppm, K 12 ppm' for messages, skipping nutrients the document lacks."""
    if not reading:
        return 'no reading'
    parts = []
    for key in ('n', 'p', 'k'):
        value = reading.get(key)
        if value is None:
            continue
        parts.append(f"{key.upper()} {value:g} ppm")
    return ', '.join(parts) or 'no reading'


def detect_npk_deficiency(reading):
    """Read a deficiency straight off the sensor values.

    Returns (deficiency, low_labels): `deficiency` is the value the fertilizer form would
    otherwise have been asked to pick by hand ('nitrogen'/'phosphorus'/'potassium', 'multiple'
    when more than one nutrient is short, or 'none'), and `low_labels` names them for display.
    """
    if not reading:
        return 'none', []

    low_keys = []
    for key in ('n', 'p', 'k'):
        value = reading.get(key)
        if value is None:
            continue
        try:
            if float(value) < NPK_LOW_PPM:
                low_keys.append(key)
        except (TypeError, ValueError):
            continue

    low_labels = [NPK_NUTRIENT_LABELS[key] for key in low_keys]
    if not low_keys:
        return 'none', low_labels
    if len(low_keys) == 1:
        return NPK_DEFICIENCY_NAMES[low_keys[0]], low_labels
    return 'multiple', low_labels


def record_season_incident(user_id, season_id, incident_type, quantity, note=''):
    """Append a hand-logged crop loss (failed germination, animal damage, ...) to the season."""
    if incident_type not in SEASON_INCIDENT_LABELS:
        return False

    client = get_firestore_client()
    if client is None:
        return False

    season_ref = client.collection('users').document(user_id).collection('seasons').document(season_id)
    season_doc = season_ref.get()
    season_data = season_doc.to_dict() or {}
    incidents = season_data.get('incidents', []) or []

    incidents.append({
        'type': incident_type,
        'quantity': quantity,
        'note': note,
        'timestamp': timezone.now().isoformat(),
    })
    season_ref.update({'incidents': incidents})
    return True


def record_disease_detection(user_id, season_id, predicted_class, confidence):
    """Upsert a detected-disease entry on the active season so the Variety Guide can check it off."""
    if predicted_class not in DISEASE_INFO or predicted_class == 'Healthy Eggplant Leaf':
        return

    client = get_firestore_client()
    if client is None:
        return

    season_ref = client.collection('users').document(user_id).collection('seasons').document(season_id)
    season_doc = season_ref.get()
    season_data = season_doc.to_dict() or {}
    detected = season_data.get('detected_diseases', []) or []

    now = timezone.now().isoformat()
    existing = next((d for d in detected if d.get('name') == predicted_class), None)
    if existing:
        existing['last_detected'] = now
        existing['last_confidence'] = confidence
        existing['count'] = existing.get('count', 1) + 1
    else:
        detected.append({
            'name': predicted_class,
            'first_detected': now,
            'last_detected': now,
            'last_confidence': confidence,
            'count': 1,
        })

    season_ref.update({'detected_diseases': detected})


def get_growth_stage_index(day, stages=None):
    """Map an elapsed season day to its index in the variety's stage guide.

    Stage day ranges overlap slightly by design (e.g. flowering 60-80 and fruiting
    75-100), so the first stage whose range covers the day wins, keeping earlier
    stages authoritative during the overlap.
    """
    stages = stages or GROWTH_STAGES
    try:
        day = int(day)
    except (TypeError, ValueError):
        day = 1
    for index, stage in enumerate(stages):
        if day <= stage['day_end']:
            return index
    return len(stages) - 1


def get_active_season(user_id):
    client = get_firestore_client()
    if client is None:
        return None

    seasons_ref = client.collection('users').document(user_id).collection('seasons')
    active_seasons = seasons_ref.where('is_active', '==', True).limit(1).get()
    if not active_seasons:
        return None

    season_doc = active_seasons[0]
    season_data = season_doc.to_dict() or {}
    season_data['id'] = season_doc.id

    # Calculate current day (safe parsing)
    start_date_raw = season_data.get('start_date')
    try:
        start_dt = datetime.fromisoformat(start_date_raw) if isinstance(start_date_raw, str) else start_date_raw
    except Exception:
        start_dt = timezone.now()

    try:
        season_data['current_day'] = (timezone.now().date() - start_dt.date()).days + 1
    except Exception:
        season_data['current_day'] = 1

    # Keep the parsed start date around so templates can format it instead of showing raw ISO text.
    season_data['start_date_display'] = start_dt

    # Derive the growth stage from elapsed days rather than trusting the stored
    # 'phase' field, which is only ever set once (to 'seedling') when the season starts.
    current_day = season_data['current_day']
    # Each variety can carry its own timeline; unknown varieties use the default one.
    variety_stages = get_growth_stages(season_data.get('variety'))
    stage_index = get_growth_stage_index(current_day, variety_stages)
    current_stage = variety_stages[stage_index]
    season_data['phase'] = current_stage['label']
    season_data['growth_stage'] = current_stage

    stages = []
    for i, stage in enumerate(variety_stages):
        status = 'done' if i < stage_index else 'current' if i == stage_index else 'upcoming'
        # days_left counts down to the end of the active stage; for later stages it counts
        # down to the day that stage begins, so every card shows a concrete number to track.
        if status == 'current':
            days_left = max(stage['day_end'] - current_day, 0)
        elif status == 'upcoming':
            days_left = max(stage['day_start'] - current_day, 0)
        else:
            days_left = 0
        stages.append({**stage, 'status': status, 'days_left': days_left})

    season_data['growth_stages'] = stages
    season_data['days_to_harvest'] = max(variety_stages[-1]['day_end'] - current_day, 0)

    # Placeholder dynamic data (to be extended with real sensors/logs)
    season_data['recommended_task'] = season_data.get('recommended_task', "Check for pests.")
    season_data['sensor_insights'] = season_data.get('sensor_insights', "No sensor data yet.")
    season_data['required_actions'] = season_data.get('required_actions', [])

    # Normalize completed activity items to strings for display.
    raw_activities = season_data.get('completed_activities', []) or []
    completed_activities = []
    for item in raw_activities:
        if isinstance(item, str):
            completed_activities.append(item)
        elif isinstance(item, dict):
            parts = []
            if 'action_type' in item:
                parts.append(item['action_type'].replace('_', ' ').capitalize())
            if 'timestamp' in item:
                parts.append(f"at {item['timestamp']}")
            if 'day_number' in item:
                parts.append(f"(day {item['day_number']})")
            completed_activities.append(' '.join(parts).strip() or str(item))
        else:
            completed_activities.append(str(item))

    # If no explicit completed activity records exist, derive them from today's action logs.
    if not completed_activities:
        actions = season_data.get('actions', []) or []
        daily_logs = season_data.get('daily_logs', []) or []
        today_day = season_data.get('current_day', 1)

        def _log_matches_today(entry):
            day = entry.get('day_number') or entry.get('day')
            try:
                return int(day) == int(today_day)
            except Exception:
                return False

        for action in actions:
            if _log_matches_today(action):
                action_type = action.get('action_type', 'activity').replace('_', ' ').capitalize()
                completed_activities.append(f"{action_type} completed")

        for log in daily_logs:
            if _log_matches_today(log):
                log_type = log.get('type') or log.get('action_type') or 'Activity'
                completed_activities.append(f"{str(log_type).replace('_', ' ').capitalize()} recorded")

    season_data['completed_activities'] = completed_activities

    return season_data


def apply_npk_recommendation(season_data, reading):
    """Turn a live NPK reading into the season's 'today's task' line.

    Kept out of get_active_season so the sensor lookup only happens on the pages that show
    the task — get_active_season itself is called by the dashboard's 5-second poll.
    """
    if not season_data or not reading:
        return season_data

    _, low_nutrients = detect_npk_deficiency(reading)
    if not low_nutrients:
        return season_data

    if 'fertilize' not in season_data.setdefault('required_actions', []):
        season_data['required_actions'].append('fertilize')

    if len(low_nutrients) > 1:
        recommendation = f"Multiple nutrient levels are low ({', '.join(low_nutrients)}). A balanced NPK fertilizer is recommended."
    else:
        recommendation = f"{low_nutrients[0]} level is low. A {low_nutrients[0]}-rich fertilizer is recommended."

    season_data['recommended_task'] = recommendation
    return season_data


def update_pump_status(user_id, pump_name, status, activated_by=None):
    client = get_firestore_client()
    if client is None:
        return False

    pump_status_ref = client.collection('users').document(user_id).collection('pump_status').document(pump_name)
    payload = {
        'pump': pump_name,
        'status': status,
        'updated_at': timezone.now().isoformat(),
        'updated_by': activated_by,
    }
    pump_status_ref.set(payload, merge=True)
    return True


def update_latest_sensor_pump_status(user_id, season_id, pump_name, status):
    client = get_firestore_client()
    if client is None:
        return False

    season_ref = client.collection('users').document(user_id).collection('seasons').document(season_id)
    sensor_collection = season_ref.collection('sensor_data')
    sensor_collection.document('pump_commands').set(
        {f'{pump_name}_status': status, 'timestamp': timezone.now().isoformat()}, merge=True)
    return True

@login_required
def home(request):
    user_id = str(request.user.id)
    
    if request.method == 'POST':
        action = request.POST.get('action')
        client = get_firestore_client()
        if client is None:
            messages.error(request, 'Firebase is not configured.')
            return redirect('home')
        if action == 'start_season':
            variety = request.POST.get('variety')
            new_season = {
                'variety': variety,
                'start_date': timezone.now().isoformat(),
                'end_date': None,
                'phase': 'seedling',
                'is_active': True,
                'daily_logs': [],
                'sensor_data': [],
                'actions': []
            }
            client.collection('users').document(user_id).collection('seasons').add(new_season)
            return redirect('home')
        elif action == 'end_season':
            active_season = get_active_season(user_id)
            if active_season:
                season_ref = client.collection('users').document(user_id).collection('seasons').document(active_season['id'])
                season_ref.update({
                    'is_active': False,
                    'end_date': timezone.now().isoformat(),
                    'phase': 'completed'
                })
            return redirect('home')

    active_season = get_active_season(user_id)
    if active_season:
        # Today's task follows the soil probe, not the mock ADC this server falls back to.
        apply_npk_recommendation(
            active_season, get_latest_npk_reading(user_id, active_season.get('id')))
    guide_items = build_disease_guide_items(active_season)

    context = {
        'season': active_season,
        'guide_items': guide_items,
        'detected_count': sum(1 for item in guide_items if item['detected']),
        'total_count': len(guide_items),
    }
    return render(request, 'home.html', context)


@login_required
@require_POST
def start_new_season(request):
    user_id = str(request.user.id)
    variety = request.POST.get('variety')

    client = get_firestore_client()
    if client is None:
        messages.error(request, 'Firebase is not configured.')
        return redirect('home')

    # Deactivate any existing active seasons
    seasons_ref = client.collection('users').document(user_id).collection('seasons')
    active_seasons = seasons_ref.where('is_active', '==', True).get()
    for season in active_seasons:
        season.reference.update({'is_active': False, 'end_date': timezone.now().isoformat()})

    # Create new season
    new_season = {
        'variety': variety,
        'start_date': timezone.now().isoformat(),
        'end_date': None,
        'phase': 'seedling',
        'is_active': True,
        'daily_logs': [],
        'sensor_data': [],
        'actions': []
    }
    client.collection('users').document(user_id).collection('seasons').add(new_season)
    messages.success(request, f'New season started with {variety}.')
    return redirect('home')




def landing_page(request):
    return render(request, 'landing_page.html')


def about(request):
    """Simple about page fallback if template missing."""
    try:
        return render(request, 'about.html')
    except Exception:
        return HttpResponse('<h1>About</h1><p>HI-TECH EGGPLANT</p>')

@login_required
def predict(request):
    image = request.FILES.get('image')
    if request.method == 'POST' and image:
        # Ensure media directory exists
        media_dir = settings.MEDIA_ROOT
        os.makedirs(media_dir, exist_ok=True)

        # Save the uploaded image
        fs = FileSystemStorage(location=media_dir, base_url=settings.MEDIA_URL)
        filename = fs.save(image.name, image)
        uploaded_file_url = fs.url(filename)

        # Make the prediction based on the uploaded image
        image_path = os.path.join(settings.MEDIA_ROOT, filename)
        predicted_class, confidence = predict_plant_disease(image_path)

        # Log activity
        ActivityHistory.objects.create(user=request.user, action='upload_predict', details=f'Predicted: {predicted_class}, Confidence: {confidence}%')

        # Check off the detected disease in the active season's Variety Guide
        user_id = str(request.user.id)
        active_season = get_active_season(user_id)
        if active_season:
            record_disease_detection(user_id, active_season['id'], predicted_class, confidence)

        # Alert the farmer by e-mail/SMS. Dispatch is threaded inside notify_disease_detection,
        # so a slow SMTP or SMS gateway never delays the redirect to the result page.
        notify_disease_detection(
            request.user, predicted_class, confidence,
            disease_info=DISEASE_INFO.get(predicted_class), source='upload')

        # Store the result in session to send it to the result page
        request.session['prediction_result'] = {
            'uploaded_file_url': uploaded_file_url,
            'predicted_class': predicted_class,
            'confidence': confidence,
        }

        return redirect('result')  # Redirect to result page

    context = {
        'default_rtmp_url': getattr(settings, 'STREAM_RTMP_BASE_URL', 'rtmp://127.0.0.1/live'),
        'default_hls_url': getattr(settings, 'STREAM_HLS_BASE_URL', 'http://127.0.0.1:8888/live'),
    }
    return render(request, 'predict.html', context)


def predict_plant_disease(image_path):
    results = model.predict(source=image_path, verbose=False)
    probabilities = results[0].probs
    predicted_class_idx = int(probabilities.top1)
    predicted_class = normalize_predicted_class(results[0].names[predicted_class_idx])
    confidence = round(float(probabilities.top1conf) * 100, 2)

    return predicted_class, confidence


def result(request):
    # Retrieve prediction result from session
    context = dict(request.session.get('prediction_result', {}) or {})
    context['disease_info'] = DISEASE_INFO.get(context.get('predicted_class'))
    return render(request, 'result.html', context)


def build_disease_guide_items(active_season):
    """Checklist items for the Variety Guide, checked off from the season's detected_diseases."""
    detected_by_name = {}
    if active_season:
        for entry in active_season.get('detected_diseases', []) or []:
            if entry.get('name'):
                detected_by_name[entry['name']] = entry

    guide_items = []
    for name in DISEASE_GUIDE_ORDER:
        info = DISEASE_INFO[name]
        detection = detected_by_name.get(name)
        guide_items.append({
            'name': name,
            'icon': info['icon'],
            'about': info['about'],
            'medicine_label': info['medicine_label'],
            'medicine': info['medicine'],
            'recommendation': info['recommendation'],
            'detected': detection is not None,
            'detection': detection,
        })
    return guide_items


def build_season_analytics(active_season):
    """Whole-season rollup: what the model detected, what was applied, and what was lost."""
    if not active_season:
        return None

    # Detections come from the Predict tool (see record_disease_detection).
    detection_rows = []
    total_detections = 0
    insect_types = set()
    disease_types = set()
    for entry in active_season.get('detected_diseases', []) or []:
        name = entry.get('name')
        if not name:
            continue
        try:
            count = int(entry.get('count', 1) or 1)
        except (TypeError, ValueError):
            count = 1
        total_detections += count

        is_insect = name in INSECT_PEST_CLASSES
        (insect_types if is_insect else disease_types).add(name)
        detection_rows.append({
            'name': name,
            'count': count,
            'is_insect': is_insect,
            'icon': DISEASE_INFO.get(name, {}).get('icon', 'circle-exclamation'),
            'last_confidence': entry.get('last_confidence'),
            'first_detected': entry.get('first_detected'),
            'last_detected': entry.get('last_detected'),
        })
    detection_rows.sort(key=lambda row: row['count'], reverse=True)

    # Treatments come from the action log written by log_action.
    actions = active_season.get('actions', []) or []
    spray_count = sum(1 for a in actions if a.get('action_type') in ('spray', 'spraying'))
    fertilizer_count = sum(1 for a in actions if a.get('action_type') in ('fertilize', 'fertilizer'))
    watering_count = sum(1 for a in actions if a.get('action_type') in ('water', 'watering'))

    # Hand-logged losses (see record_season_incident).
    totals_by_type = {item['key']: 0 for item in SEASON_INCIDENT_TYPES}
    plants_lost = 0
    incident_log = []
    for entry in active_season.get('incidents', []) or []:
        key = entry.get('type')
        if key not in totals_by_type:
            continue
        try:
            quantity = int(entry.get('quantity', 0) or 0)
        except (TypeError, ValueError):
            quantity = 0
        totals_by_type[key] += quantity
        plants_lost += quantity
        incident_log.append({
            'label': SEASON_INCIDENT_LABELS[key]['label'],
            'icon': SEASON_INCIDENT_LABELS[key]['icon'],
            'quantity': quantity,
            'note': entry.get('note', ''),
            'timestamp': entry.get('timestamp'),
        })
    incident_log.reverse()  # newest first

    incident_rows = [
        {**item, 'quantity': totals_by_type[item['key']]}
        for item in SEASON_INCIDENT_TYPES
    ]

    return {
        'total_detections': total_detections,
        'insect_type_count': len(insect_types),
        'disease_type_count': len(disease_types),
        'detection_rows': detection_rows,
        'spray_count': spray_count,
        'fertilizer_count': fertilizer_count,
        'watering_count': watering_count,
        'plants_lost': plants_lost,
        'seeds_not_germinated': totals_by_type['seeds_not_germinated'],
        'eaten_by_animals': totals_by_type['eaten_by_birds'] + totals_by_type['eaten_by_animals'],
        'incident_rows': incident_rows,
        'incident_log': incident_log,
    }


@login_required
@require_POST
def log_season_incident(request):
    """Record a hand-observed crop loss against the active season, then return to the guide."""
    user_id = str(request.user.id)
    active_season = get_active_season(user_id)
    if not active_season:
        messages.error(request, 'No active season. Start a new season first.')
        return redirect('variety_guide')

    incident_type = request.POST.get('incident_type')
    if incident_type not in SEASON_INCIDENT_LABELS:
        messages.error(request, 'Please choose a valid incident type.')
        return redirect('variety_guide')

    try:
        quantity = int(request.POST.get('quantity', 1))
    except (TypeError, ValueError):
        quantity = 1
    quantity = max(quantity, 1)

    note = (request.POST.get('note') or '').strip()[:300]

    if record_season_incident(user_id, active_season['id'], incident_type, quantity, note):
        label = SEASON_INCIDENT_LABELS[incident_type]['label']
        ActivityHistory.objects.create(
            user=request.user,
            action='season_incident',
            details=f'{label}: {quantity} plant(s). {note}'.strip(),
        )
        messages.success(request, f'Logged: {label} ({quantity}).')
    else:
        messages.error(request, 'Could not save the incident. Check the Firebase configuration.')

    return redirect('variety_guide')


@login_required
def variety_guide(request):
    """Philippine eggplant varieties with season health and disease tracking."""
    user_id = str(request.user.id)
    active_season = get_active_season(user_id)
    guide_items = build_disease_guide_items(active_season)

    context = {
        'season': active_season,
        'guide_items': guide_items,
        'detected_count': sum(1 for item in guide_items if item['detected']),
        'total_count': len(guide_items),
        'analytics': build_season_analytics(active_season),
        'incident_types': SEASON_INCIDENT_TYPES,
    }
    return render(request, 'variety_guide.html', context)
def signup(request):
    return redirect('login')

# Login view
def login_view(request):
    if request.method == 'POST':
        username = request.POST['username']
        password = request.POST['password']
        user = authenticate(request, username=username, password=password)
        if user is not None:
            login(request, user)
            return redirect('home')
        else:
            messages.error(request, 'Invalid username or password.')
    return render(request, 'login.html', {
        'google_client_id': '',
        'google_setup_hint': False,
    })

# Logout view




@login_required
def profile(request):
    return render(request, 'profile.html')
@login_required
def edit_profile(request):
    return redirect('profile')

def logout_view(request):
    logout(request)
    return redirect('landing_page')

@login_required
@require_POST
def log_action(request):
    """Log a user action (water, spray, fertilize) to Firestore then redirect to the target module."""
    user_id = str(request.user.id)
    action_type = request.POST.get('action_type')
    next_url = request.POST.get('next') or request.POST.get('redirect_to') or '/home/'

    client = get_firestore_client()
    if client is None:
        messages.error(request, 'Firebase is not configured.')
        return redirect('home')

    active_season = get_active_season(user_id)
    if not active_season:
        messages.error(request, 'No active season. Start a new season first.')
        return redirect('home')

    season_ref = client.collection('users').document(user_id).collection('seasons').document(active_season['id'])

    # Build action record
    timestamp = timezone.now().isoformat()
    day_number = active_season.get('current_day', 1)
    action_record = {
        'timestamp': timestamp,
        'day_number': day_number,
        'action_type': action_type,
        'user_id': user_id,
    }

    # Append to actions and daily_logs
    season_doc = season_ref.get()
    season_data = season_doc.to_dict() or {}
    actions = season_data.get('actions', [])
    daily_logs = season_data.get('daily_logs', [])

    actions.append(action_record)
    daily_logs.append({
        'timestamp': timestamp,
        'day': day_number,
        'type': action_type,
    })

    season_ref.update({
        'actions': actions,
        'daily_logs': daily_logs,
    })

    # Write command to Firestore for Raspberry Pi to execute
    command_record = {
        'user_id': user_id,
        'season_id': active_season['id'],
        'command_type': action_type,  # 'water', 'spray', 'fertilize'
        'duration': 5,  # Default 5 seconds
        'timestamp_issued': timestamp,
        'executed': False,
        'executed_timestamp': None,
    }
    
    # Write to commands collection for Raspberry Pi to poll
    commands_ref = client.collection('users').document(user_id).collection('commands')
    commands_ref.add(command_record)
    
    # Also update pump status in the current sensor data or create pump status record
    if action_type in ('water', 'spray'):
        pump_name = 'water_pump' if action_type == 'water' else 'spray_pump'
        # Create/update pump status document
        pump_status_ref = client.collection('users').document(user_id).collection('pump_status').document(pump_name)
        pump_status_ref.set({
            'pump': pump_name,
            'status': 'active',
            'activated_at': timestamp,
            'activated_by': request.user.username,
        }, merge=True)
        update_latest_sensor_pump_status(user_id, active_season['id'], pump_name, 'active')

    try:
        # If next_url looks like a path already, just redirect
        if next_url.startswith('/'):
            return redirect(next_url)
        return redirect(next_url)
    except Exception:
        return redirect('home')


@login_required
def season_history(request):
    """Render a page listing past seasons and key stats for the user."""
    user_id = str(request.user.id)
    client = get_firestore_client()
    if client is None:
        messages.error(request, 'Firebase is not configured.')
        return redirect('home')

    seasons_ref = client.collection('users').document(user_id).collection('seasons')
    seasons_docs = seasons_ref.get()
    seasons = []
    for doc in seasons_docs:
        data = doc.to_dict() or {}
        start = data.get('start_date')
        end = data.get('end_date')
        try:
            start_dt = datetime.fromisoformat(start) if isinstance(start, str) else start
        except Exception:
            start_dt = None
        try:
            end_dt = datetime.fromisoformat(end) if isinstance(end, str) else end
        except Exception:
            end_dt = None

        if end_dt and start_dt:
            duration = (end_dt.date() - start_dt.date()).days + 1
        elif start_dt:
            duration = (timezone.now().date() - start_dt.date()).days + 1
        else:
            duration = None

        actions = data.get('actions', []) or []
        watering = sum(1 for a in actions if a.get('action_type') == 'water')
        spraying = sum(1 for a in actions if a.get('action_type') in ('spray', 'spraying'))
        fertilize = sum(1 for a in actions if a.get('action_type') in ('fertilize', 'fertilizer'))
        disease_events = sum(1 for a in actions if a.get('action_type') in ('disease', 'detection'))

        seasons.append({
            'id': doc.id,
            'variety': data.get('variety'),
            'start_date': start,
            'end_date': end,
            'phase': data.get('phase'),
            'is_active': data.get('is_active', False),
            'duration_days': duration,
            'watering_count': watering,
            'spraying_count': spraying,
            'fertilizer_count': fertilize,
            'disease_occurrences': disease_events,
        })

    # sort so most recent first by start_date if available
    seasons.sort(key=lambda s: s.get('start_date') or '', reverse=True)

    return render(request, 'season_history.html', {'seasons': seasons})


@login_required
def api_today_summary(request):
    """Return JSON with today's summary for the active season (only today's data)."""
    user_id = str(request.user.id)
    client = get_firestore_client()
    if client is None:
        return JsonResponse({'error': 'Firebase not configured.'}, status=500)

    active = get_active_season(user_id)
    if not active:
        return JsonResponse({'active': False, 'message': 'No active season'})

    # Same sensor-derived task the dashboard shows.
    apply_npk_recommendation(active, get_latest_npk_reading(user_id, active.get('id')))

    # Build minimal payload with only today's data per requirements
    payload = {
        'active': True,
        'variety': active.get('variety'),
        'phase': active.get('phase'),
        'current_day': active.get('current_day', 1),
        'recommended_task': active.get('recommended_task'),
        'completed_activities': active.get('completed_activities', []),
        'sensor_insights': active.get('sensor_insights'),
        'required_actions': active.get('required_actions', []),
    }
    return JsonResponse(payload)

@login_required
def live_predict(request):
    if request.method == 'POST':
        image_data = request.POST.get('image')
        if image_data:
            # Decode the base64 image
            format, imgstr = image_data.split(',')
            img_data = base64.b64decode(imgstr)
            try:
                img = Image.open(BytesIO(img_data))
            except Exception as e:
                return JsonResponse({'error': f'Invalid image data: {str(e)}'}, status=400)

            # Make prediction — the model carries its own class names, so read them off the
            # result instead of keeping a copy of the label list here.
            results = model.predict(source=img.convert('RGB'), verbose=False)
            probabilities = results[0].probs
            predicted_class = normalize_predicted_class(results[0].names[int(probabilities.top1)])
            confidence = round(float(probabilities.top1conf) * 100, 2)

            # Generate recommendation
            if predicted_class == "Healthy Eggplant Leaf":
                recommendation = "No action needed. Plant is healthy."
            else:
                info = DISEASE_INFO.get(predicted_class)
                recommendation = info['recommendation'] if info else "Disease detected. Consider treatment or removal."

            # Automatic spraying if disease detected
            if predicted_class != "Healthy Eggplant Leaf":
                spray_pump.on()
                time.sleep(5)  # Spray for 5 seconds
                spray_pump.off()
                ActivityHistory.objects.create(user=request.user, action='automatic_spray', details=f'Disease: {predicted_class}, Confidence: {confidence}%')

            return JsonResponse({'predicted_class': predicted_class, 'confidence': confidence, 'recommendation': recommendation})
    return JsonResponse({'error': 'Invalid request'}, status=400)


@login_required
@require_POST
def start_live_scan(request):
    """Start a background ffmpeg process to publish a live stream to the provided RTMP URL + stream key.

    Expected JSON body: { "rtmp_url": "rtmp://host/app", "stream_key": "key123", "device": "diji_neo" }
    Requires settings.LIVE_CAPTURE_SOURCE (or environment LIVE_CAPTURE_SOURCE) to be set to the ffmpeg input source (e.g., an RTSP URL, /dev/video0, or other supported input).
    """
    try:
        payload = json.loads(request.body.decode('utf-8') or '{}')
    except Exception:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    rtmp_url = payload.get('rtmp_url')
    stream_key = payload.get('stream_key')
    device = payload.get('device')

    if not rtmp_url or not stream_key:
        return JsonResponse({'error': 'rtmp_url and stream_key are required'}, status=400)

    publish_url = rtmp_url.rstrip('/') + '/' + stream_key
    user_id = str(request.user.id)

    # If a stream is already running for this user, reject
    existing = live_streams.get(user_id)
    if existing and existing.poll() is None:
        return JsonResponse({'ok': False, 'message': 'A live stream is already running for this user.'}, status=400)

    # DJI drone streams publish directly to RTMP; they should not be blocked by local capture configuration.
    device_name = (device or '').lower()
    source = payload.get('source') or getattr(settings, 'LIVE_CAPTURE_SOURCE', None) or os.environ.get('LIVE_CAPTURE_SOURCE')
    if device_name in {'diji_neo', 'dji neo', 'drone', 'drone_rtmp'}:
        try:
            ActivityHistory.objects.create(user=request.user, action='start_live_stream', details=f'drone_rtmp:{publish_url}')
        except Exception:
            logging.exception('Failed to log ActivityHistory for drone RTMP live stream')
        return JsonResponse({
            'ok': True,
            'mode': 'drone_rtmp',
            'publish_url': publish_url,
            'rtmp_url': rtmp_url,
            'stream_key': stream_key,
            'device': device,
            'message': 'Drone RTMP stream is configured. Use the RTMP base URL and stream key on the drone.'
        })

    if not source:
        return JsonResponse({
            'ok': False,
            'error': 'No capture source configured for the local web stream. Set LIVE_CAPTURE_SOURCE or provide a source URL for local publishing.',
            'code': 'missing_capture_source'
        }, status=400)

    ffmpeg_bin = resolve_ffmpeg_binary()
    if not os.path.exists(ffmpeg_bin) and ffmpeg_bin == 'ffmpeg':
        return JsonResponse({
            'ok': False,
            'error': 'ffmpeg is not installed or is not on the PATH. Install FFmpeg to enable local stream publishing.',
            'code': 'ffmpeg_missing'
        }, status=400)

    # Build ffmpeg command. Handle RTSP and local camera sources explicitly so the start_live_scan flow works
    # with both network streams and webcam inputs.
    if source.startswith('rtsp://') or source.startswith('http://') or source.startswith('https://'):
        cmd = [ffmpeg_bin, '-re', '-i', source, '-c:v', 'libx264', '-preset', 'veryfast', '-tune', 'zerolatency', '-c:a', 'aac', '-b:a', '128k', '-vf', 'scale=1280:-2', '-maxrate', '3000k', '-bufsize', '6000k', '-g', '50', '-f', 'flv', publish_url]
    elif source.startswith('/dev/video') or source.startswith('video=') or source.isdigit():
        if os.name == 'nt':
            cmd = [ffmpeg_bin, '-f', 'dshow', '-i', source, '-c:v', 'libx264', '-preset', 'veryfast', '-tune', 'zerolatency', '-c:a', 'aac', '-b:a', '128k', '-vf', 'scale=1280:-2', '-maxrate', '3000k', '-bufsize', '6000k', '-g', '50', '-f', 'flv', publish_url]
        else:
            cmd = [ffmpeg_bin, '-f', 'v4l2', '-i', source, '-c:v', 'libx264', '-preset', 'veryfast', '-tune', 'zerolatency', '-c:a', 'aac', '-b:a', '128k', '-vf', 'scale=1280:-2', '-maxrate', '3000k', '-bufsize', '6000k', '-g', '50', '-f', 'flv', publish_url]
    else:
        cmd = [ffmpeg_bin, '-re', '-i', source, '-c:v', 'libx264', '-preset', 'veryfast', '-tune', 'zerolatency', '-c:a', 'aac', '-b:a', '128k', '-vf', 'scale=1280:-2', '-maxrate', '3000k', '-bufsize', '6000k', '-g', '50', '-f', 'flv', publish_url]

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        live_streams[user_id] = proc
        logging.info(f"Started ffmpeg publish for user {user_id}, pid={proc.pid} target={publish_url}")
        try:
            ActivityHistory.objects.create(user=request.user, action='start_live_stream', details=publish_url)
        except Exception:
            logging.exception('Failed to log ActivityHistory for start_live_stream')
        return JsonResponse({'ok': True, 'pid': proc.pid})
    except Exception as e:
        logging.exception('Failed to start ffmpeg process')
        return JsonResponse({'ok': False, 'error': str(e)}, status=500)


@login_required
@require_POST
def stop_live_scan(request):
    """Stop a running live stream started by start_live_scan for the logged-in user."""
    user_id = str(request.user.id)
    proc = live_streams.get(user_id)
    if not proc:
        return JsonResponse({'ok': False, 'message': 'No running stream found for this user.'}, status=400)

    try:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        del live_streams[user_id]
        try:
            ActivityHistory.objects.create(user=request.user, action='stop_live_stream', details='stopped')
        except Exception:
            logging.exception('Failed to log ActivityHistory for stop_live_stream')
        return JsonResponse({'ok': True})
    except Exception as e:
        logging.exception('Error stopping live stream')
        return JsonResponse({'ok': False, 'error': str(e)}, status=500)

def watering_thread_func():
    while watering_schedule['active']:
        start_dt = watering_schedule.get('start_datetime')
        now = timezone.now()

        if start_dt is None or now >= start_dt:
            water_pump.on()
            time.sleep(watering_schedule.get('duration', 5))
            water_pump.off()
            watering_schedule['start_datetime'] = _next_daily_6am_datetime(start_dt or now)
            continue

        wait_seconds = (start_dt - now).total_seconds()
        time.sleep(min(wait_seconds, 60))


# Scheduled sprays go out early morning, before the heat of the day.
SCHEDULED_SPRAY_HOUR = 6
SCHEDULED_SPRAY_SECONDS = 5
SPRAY_TIMEZONE = ZoneInfo('Asia/Manila')


def scheduled_spray_datetime(due_date):
    """The moment a chemical application due on this date should spray."""
    start_dt = datetime.combine(due_date, datetime.min.time()).replace(hour=SCHEDULED_SPRAY_HOUR)
    if timezone.is_naive(start_dt):
        try:
            start_dt = timezone.make_aware(start_dt, SPRAY_TIMEZONE)
        except Exception:
            pass
    return start_dt


def next_pending_chemical_application(user):
    """The soonest chemical task still waiting to be done, or None."""
    return ScheduledApplication.objects.filter(
        user=user,
        application_type='chemical',
        completed=False,
        due_date__gte=timezone.localtime(timezone.now(), SPRAY_TIMEZONE).date(),
    ).order_by('due_date').first()


def sync_spraying_schedule_with_task(user):
    """Return task context without rearming completed or disabled RTDB schedules."""
    return next_pending_chemical_application(user)


def arm_automated_spraying(user, due_date, reason=''):
    start_dt = scheduled_spray_datetime(due_date)
    save_spray_schedule(str(user.id), True, start_dt, SCHEDULED_SPRAY_SECONDS, user.username)
    ActivityHistory.objects.create(
        user=user, action='update_spray_schedule',
        details=f'Armed in Realtime Database ({reason}); start {start_dt.isoformat()}')
    return start_dt


def _spray_schedule_request(request, action, redirect_name):
    try:
        user_id = str(request.user.id)
        schedule = get_spray_schedule(user_id)
        if action == 'toggle_spray_schedule':
            enabled = not schedule['active']
            if enabled:
                task = next_pending_chemical_application(request.user)
                start = schedule['start_datetime']
                if start is None or schedule.get('executed_id') == schedule.get('id'):
                    start = scheduled_spray_datetime(task.due_date) if task else timezone.now()
                save_spray_schedule(user_id, True, start, SCHEDULED_SPRAY_SECONDS, request.user.username)
            else:
                disable_spray_schedule(user_id)
            details = f'Automated spraying {"enabled" if enabled else "disabled"} in Realtime Database.'
        else:
            with timezone.override(SPRAY_TIMEZONE):
                start = _parse_schedule_datetime(request.POST.get('spray_start_datetime'))
            if start is None:
                raise ValueError('Choose a valid spraying start date and time.')
            save_spray_schedule(user_id, schedule['active'], start, SCHEDULED_SPRAY_SECONDS, request.user.username)
            details = f'Spraying schedule saved in Realtime Database: {start.isoformat()}.'
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect(redirect_name)
    except Exception:
        logging.exception('Could not save Realtime Database spraying schedule')
        messages.error(request, 'Could not save spraying schedule. Check Firebase configuration and connection.')
        return redirect(redirect_name)
    ActivityHistory.objects.create(user=request.user, action=action, details=details)
    messages.success(request, details)
    return redirect(redirect_name)


def _spray_schedule_context(request):
    try:
        schedule = get_spray_schedule(str(request.user.id))
    except Exception:
        logging.exception('Could not read Realtime Database spraying schedule')
        messages.warning(request, 'Spraying schedule unavailable. Check Firebase connection.')
        schedule = {'active': False, 'duration': 5, 'start_datetime': None}
    return {'spraying_schedule': schedule,
            'spraying_start_datetime': timezone.localtime(
                schedule['start_datetime'] or timezone.now(), SPRAY_TIMEZONE).strftime('%Y-%m-%dT%H:%M')}


@login_required
def history(request):
    history = ActivityHistory.objects.filter(user=request.user).order_by('-timestamp')
    return render(request, 'history.html', {'history': history})


def _manual_pump_request(request, action, redirect_name):
    pump_name = 'water_pump' if action == 'manual_water' else 'spray_pump'
    label = 'watering' if action == 'manual_water' else 'spraying'
    try:
        raw_enabled = request.POST.get('enabled')
        if raw_enabled not in ('true', 'false'):
            raise ValueError('Choose ON or OFF and submit again.')
        enabled = raw_enabled == 'true'
        set_manual_pump(str(request.user.id), pump_name, enabled, request.user.username)
    except Exception as exc:
        logging.getLogger(__name__).warning('Realtime Database pump request failed: %s', exc)
        messages.error(request, f'Could not send {label} request. Check Realtime Database configuration and connection.')
        return redirect(redirect_name)
    ActivityHistory.objects.create(user=request.user, action=action,
                                   details=f'Manual {label} requested {"on" if enabled else "off"}')
    messages.success(request, f'{label.capitalize()} {"ON" if enabled else "OFF"} request sent to Raspberry Pi.')
    return redirect(redirect_name)


def _manual_pump_context(request):
    try:
        states = get_manual_pump_states(str(request.user.id))
    except Exception as exc:
        logging.getLogger(__name__).warning('Realtime Database pump state unavailable: %s', exc)
        messages.warning(request, 'Pump controls unavailable. Check FIREBASE_DATABASE_URL and Firebase connection.')
        states = {}
    return {'manual_watering_active': states.get('water_pump', False),
            'manual_spraying_active': states.get('spray_pump', False)}


@login_required
def automated_watering(request):
    global watering_thread
    if request.method == 'POST':
        action = request.POST.get('action')
        if action in ('manual_water', 'manual_spray'):
            return _manual_pump_request(request, action, 'automated_watering')
        if action in ('toggle_spray_schedule', 'update_spray_schedule'):
            return _spray_schedule_request(request, action, 'automated_watering')
        user_id = str(request.user.id)
        active_season = get_active_season(user_id)
        season_id = active_season['id'] if active_season else None
        if action == 'toggle_water_schedule':
            watering_schedule['active'] = not watering_schedule['active']
            if watering_schedule['active'] and watering_schedule['start_datetime'] is None:
                watering_schedule['start_datetime'] = _next_daily_6am_datetime(timezone.now())
            status = 'enabled' if watering_schedule['active'] else 'disabled'
            if watering_schedule['active']:
                watering_thread = threading.Thread(target=watering_thread_func)
                watering_thread.start()
                update_pump_status(user_id, 'water_pump', 'scheduled', request.user.username)
                if season_id:
                    update_latest_sensor_pump_status(user_id, season_id, 'water_pump', 'scheduled')
            else:
                if watering_thread and watering_thread.is_alive():
                    watering_thread.join()
                update_pump_status(user_id, 'water_pump', 'inactive', request.user.username)
                if season_id:
                    update_latest_sensor_pump_status(user_id, season_id, 'water_pump', 'inactive')
            ActivityHistory.objects.create(user=request.user, action='toggle_water_schedule', details=f'Status: {status}')
            messages.success(request, f'Automated watering {status}.')
        elif action == 'update_water_schedule':
            duration = int(request.POST.get('water_duration', 5))
            _ = _parse_schedule_datetime(request.POST.get('water_start_datetime'))
            watering_schedule['duration'] = duration
            watering_schedule['start_datetime'] = _next_daily_6am_datetime(timezone.now())
            ActivityHistory.objects.create(
                user=request.user,
                action='update_water_schedule',
                details=f'Duration: {duration}s, Start: {watering_schedule["start_datetime"].isoformat()}'
            )
            messages.success(request, 'Watering schedule updated for 6:00 AM daily.')
        elif action in ('check_connection', 'check_sensors'):
            feed = current_sensor_feed(str(request.user.id))
            if feed['pi_online']:
                messages.success(request, f"Sensor feed Online. Soil: {feed['soil_moisture']}%, Temperature: {feed['temperature']} C, Humidity: {feed['humidity']}%.")
            else:
                messages.warning(request, 'Sensor feed Offline. No fresh readings; displayed readings are zero.')
        return redirect('automated_watering')

    feed = current_sensor_feed(str(request.user.id))
    soil_moisture_percent = feed['soil_moisture']
    temperature = feed['temperature']
    humidity = feed['humidity']

    # Determine statuses
    if soil_moisture_percent < 30:
        soil_status = "Dry"
    elif soil_moisture_percent < 60:
        soil_status = "Moderate"
    else:
        soil_status = "Wet"

    if temperature < 15:
        climate_status = "Cool"
    elif temperature > 35:
        climate_status = "Hot"
    else:
        climate_status = "Optimal"

    if not feed['pi_online']:
        soil_status = climate_status = 'Offline'

    # Get history for the user
    history = ActivityHistory.objects.filter(user=request.user)[:20]  # Last 20 activities

    context = {
        'watering_schedule': watering_schedule,
        'watering_start_datetime': _format_schedule_datetime(watering_schedule.get('start_datetime')),
        **_spray_schedule_context(request),
        **_manual_pump_context(request),
        'is_raspberry_pi': is_raspberry_pi,
        'history': history,
        'pi_online': feed['pi_online'],
        'soil_moisture': soil_moisture_percent,
        'temperature': temperature,
        'humidity': humidity,
        'soil_status': soil_status,
        'climate_status': climate_status,
    }
    return render(request, 'automated_watering.html', context)

@login_required
def automated_spraying(request):
    if request.method == 'POST':
        action = request.POST.get('action')
        if action in ('manual_water', 'manual_spray'):
            return _manual_pump_request(request, action, 'automated_spraying')
        if action in ('toggle_spray_schedule', 'update_spray_schedule'):
            return _spray_schedule_request(request, action, 'automated_spraying')
        if action == 'check_connection':
            # Simple connection check - in real implementation, this would ping the Raspberry Pi
            connection_status = is_raspberry_pi  # For now, just check if GPIO is available
            if connection_status:
                messages.success(request, 'Raspberry Pi connection is active.')
            else:
                messages.warning(request, 'Raspberry Pi not detected. Running in development mode.')
            ActivityHistory.objects.create(user=request.user, action='check_connection', details=f'Connection status: {"Connected" if connection_status else "Not Connected"}')
        return redirect('automated_spraying')

    # Task context is informational; opening the page never rearms a saved schedule.
    next_chemical_application = sync_spraying_schedule_with_task(request.user)

    context = {
        **_spray_schedule_context(request),
        **_manual_pump_context(request),
        'is_raspberry_pi': is_raspberry_pi,
        'next_chemical_application': next_chemical_application,
    }
    return render(request, 'automated_spraying.html', context)

@login_required
def fertilizer_application(request):
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'apply_fertilizer':
            plant_age = int(request.POST.get('plant_age', 0))
            deficiency = request.POST.get('deficiency', 'auto')
            due_date_str = request.POST.get('due_date')

            # Read the NPK probe rather than the on-board ADC: this server is not the Pi, so
            # n_sensor/p_sensor/k_sensor here are the mock objects, not the soil.
            reading = get_latest_npk_reading(str(request.user.id))
            n_level = reading.get('n') if reading else None
            p_level = reading.get('p') if reading else None
            k_level = reading.get('k') if reading else None

            # 'auto' is the default: the deficiency comes from the probe instead of being
            # picked by hand. The manual options stay available for magnesium and iron, which
            # the NPK sensor cannot measure.
            if deficiency == 'auto':
                deficiency, low_labels = detect_npk_deficiency(reading)
                if reading is None:
                    messages.warning(request, 'No NPK reading from the Raspberry Pi yet — the recommendation below is not based on live soil data.')
                elif low_labels:
                    messages.info(request, f"Sensor reading: {_format_npk_reading(reading)} — low {', '.join(low_labels)}.")
                else:
                    messages.info(request, f"Sensor reading: {_format_npk_reading(reading)} — no nutrient is below {NPK_LOW_PPM} ppm.")

            # Fertilizer recommendations based on plant age, deficiency and NPK levels
            recommendations = get_fertilizer_recommendations(plant_age, deficiency, n_level, p_level, k_level)

            # Ensure we store/display a string for DB fields; recommendations may be a list
            if isinstance(recommendations, (list, tuple)):
                rec_text = "\n".join(recommendations)
            else:
                rec_text = str(recommendations)

            # Hand the card the lines plus what kind of fertilizer each one is, so it can show
            # the type as a label and offer to schedule this exact recommendation.
            request.session['last_recommendation'] = {
                'kind': 'fertilizer',
                'plant_age': plant_age,
                'issue': deficiency,
                'issue_label': issue_description(deficiency),
                'reco_items': build_recommendation_items(recommendations, fertilizer_product_type),
            }

            if request.POST.get('schedule') == '1' and not due_date_str:
                messages.error(request, 'Pick a schedule date before setting a fertilizer schedule.')
            elif due_date_str:
                due_date = datetime.strptime(due_date_str, '%Y-%m-%d').date()
                application = ScheduledApplication.objects.create(
                    user=request.user,
                    application_type='fertilizer',
                    plant_age=plant_age,
                    deficiency=deficiency,
                    recommendations=rec_text,
                    due_date=due_date
                )
                notify_scheduled_application(request.user, application)
                messages.success(request, f'Fertilizer application scheduled for {due_date_str}.')
            else:
                ActivityHistory.objects.create(
                    user=request.user,
                    action='fertilizer_application',
                    details=f'Plant age: {plant_age} weeks, Deficiency: {deficiency}, Recommendations: {rec_text}'
                )
                # Show concise message; actual recommendation lines are passed via messages when requested
                messages.success(request, f'Fertilizer recommendations generated for {plant_age}-week old plants with {deficiency} deficiency.')

        elif action == 'apply_chemical':
            plant_age = int(request.POST.get('plant_age', 0))
            deficiency = request.POST.get('deficiency', 'none')
            due_date_str = request.POST.get('due_date_chemical')

            # The issue is a detected disease as often as it is a nutrient deficiency now,
            # so describe it plainly instead of calling everything a deficiency.
            issue_label = 'general treatment' if deficiency == 'none' else deficiency
            if deficiency in ('nitrogen', 'phosphorus', 'potassium', 'magnesium', 'iron'):
                issue_label = f'{deficiency} deficiency'

            # Chemical application recommendations
            recommendations = get_chemical_recommendations(plant_age, deficiency)

            # Put the individual lines in front of the farmer, each tagged with what kind of
            # chemical it is: the summary message alone never showed what to apply.
            request.session['last_recommendation'] = {
                'kind': 'chemical',
                'plant_age': plant_age,
                'issue': deficiency,
                'issue_label': issue_label,
                'reco_items': build_recommendation_items(recommendations.split('; '), chemical_product_type),
            }

            if request.POST.get('schedule') == '1' and not due_date_str:
                messages.error(request, 'Pick a schedule date before setting a chemical schedule.')
            elif due_date_str:
                due_date = datetime.strptime(due_date_str, '%Y-%m-%d').date()
                application = ScheduledApplication.objects.create(
                    user=request.user,
                    application_type='chemical',
                    plant_age=plant_age,
                    deficiency=deficiency,
                    recommendations=recommendations,
                    due_date=due_date
                )
                notify_scheduled_application(request.user, application)
                messages.success(request, f'Chemical application scheduled for {due_date_str} ({issue_label}).')

                # A chemical schedule is a spraying job: arm the sprayer for that morning so the
                # farmer does not have to set the same date again on the Chemical Spraying page.
                try:
                    armed_at = arm_automated_spraying(request.user, due_date, issue_label)
                    messages.success(request,
                        f'Automated spraying saved in Realtime Database for '
                        f'{timezone.localtime(armed_at, SPRAY_TIMEZONE):%b %d, %Y %I:%M %p} Manila time.')
                except Exception:
                    logging.exception('Chemical task saved but RTDB spray arming failed')
                    messages.error(request, 'Chemical task saved, but automatic spraying could not be armed. Retry on the Chemical Spraying page.')
            else:
                ActivityHistory.objects.create(
                    user=request.user,
                    action='chemical_application',
                    details=f'Plant age: {plant_age} weeks, Issue: {issue_label}, Recommendations: {recommendations}'
                )
                messages.success(request, f'Chemical application recommendations generated for {plant_age}-week old plants ({issue_label}).')

        elif action == 'get_npk_recommendation':
            # Recommendations read straight off the soil probe — nothing is chosen by hand here.
            reading = get_latest_npk_reading(str(request.user.id))

            if reading is None:
                messages.warning(request, 'No NPK reading has been received from the Raspberry Pi yet. Check that the device is online, then try again.')
            else:
                deficiency, low_labels = detect_npk_deficiency(reading)
                summary = _format_npk_reading(reading)
                if not reading['is_fresh']:
                    age = reading.get('age_seconds')
                    when = f"{int(age // 60)} min ago" if age is not None else 'a while ago'
                    messages.warning(request, f'Using the last reading received ({when}) — the Raspberry Pi is not reporting right now.')

                if low_labels:
                    messages.success(request, f"Soil reading: {summary}. Low: {', '.join(low_labels)}.")
                else:
                    messages.success(request, f"Soil reading: {summary}. All nutrients are at or above {NPK_LOW_PPM} ppm.")

                recommendations = get_fertilizer_recommendations(
                    0, deficiency, reading.get('n'), reading.get('p'), reading.get('k'))

                request.session['last_recommendation'] = {
                    'kind': 'fertilizer',
                    'plant_age': season_plant_age_weeks(get_active_season(str(request.user.id))) or 0,
                    'issue': deficiency,
                    'issue_label': issue_description(deficiency),
                    'reco_items': build_recommendation_items(recommendations, fertilizer_product_type),
                }

                ActivityHistory.objects.create(
                    user=request.user,
                    action='npk_recommendation',
                    details=f"Auto NPK recommendation from sensor ({summary}); deficiency: {deficiency}; {', '.join(recommendations) or 'no recommendation'}"
                )

        elif action == 'complete_application':
            application_id = request.POST.get('application_id')
            try:
                application = ScheduledApplication.objects.get(id=application_id, user=request.user)
                application.completed = True
                application.save()
                ActivityHistory.objects.create(
                    user=request.user,
                    action='completed_application',
                    details=f'Completed {application.application_type} application: {application.recommendations[:50]}...'
                )
                messages.success(request, f'{application.application_type.title()} application marked as completed.')
            except ScheduledApplication.DoesNotExist:
                messages.error(request, 'Application not found.')

        # Come back to the tab the farmer submitted from. Without this the redirect reset the
        # page to the Fertilizer tab and the chemical recommendation scrolled past unseen.
        tab = 'chemical' if action == 'apply_chemical' else request.POST.get('tab') or 'fertilizer'
        return redirect(f"{reverse('fertilizer_application')}?tab={tab}#recommendations")

    # Get scheduled applications and history for the user
    scheduled_applications = ScheduledApplication.objects.filter(
        user=request.user,
        completed=False,
        due_date__gte=timezone.now().date()
    ).order_by('due_date')

    history = ActivityHistory.objects.filter(user=request.user)[:20]  # Last 20 activities

    # Process history for display
    for activity in history:
        activity.action_display = activity.action.replace('_', ' ').title()

    # Get today's and tomorrow's date for template
    today = timezone.now().date()
    tomorrow = today + timezone.timedelta(days=1)

    # Both forms are filled in from what the app already knows: the plant's age comes from the
    # growth tracker's day count, and the issue from the newest detection the Predict tool made.
    active_season = get_active_season(str(request.user.id))
    plant_age_weeks = season_plant_age_weeks(active_season)
    latest_detection = get_latest_disease_detection(active_season)
    detected_issue = latest_detection.get('name') if latest_detection else None
    if detected_issue == 'Healthy Eggplant Leaf':
        detected_issue = None

    active_tab = 'chemical' if request.GET.get('tab') == 'chemical' else 'fertilizer'

    # The last recommendation stays in the session so the card can keep showing it — with its
    # product types and its own schedule form — instead of losing it on the next page load.
    last_recommendation = request.session.get('last_recommendation')
    if last_recommendation:
        last_recommendation = dict(last_recommendation)
        last_recommendation['date_field'] = (
            'due_date_chemical' if last_recommendation.get('kind') == 'chemical' else 'due_date')
        last_recommendation['action'] = (
            'apply_chemical' if last_recommendation.get('kind') == 'chemical' else 'apply_fertilizer')

    context = {
        'is_raspberry_pi': is_raspberry_pi,
        'history': history,
        'scheduled_applications': scheduled_applications,
        'today': today,
        'tomorrow': tomorrow,
        'active_tab': active_tab,
        'last_recommendation': last_recommendation,
        'season': active_season,
        'plant_age_options': PLANT_AGE_OPTIONS,
        'plant_age_weeks': plant_age_weeks,
        'plant_age_default': plant_age_option_for(plant_age_weeks),
        'disease_issue_options': DISEASE_GUIDE_ORDER,
        'latest_detection': latest_detection,
        'detected_issue': detected_issue,
    }
    return render(request, 'fertilizer_application.html', context)


# What kind of product each recommendation line is, so the card says what to buy and not just
# a brand name. Matched as substrings, first rule wins, so order the specific ones first.
FERTILIZER_PRODUCT_TYPES = [
    ('14-14-14', 'Complete fertilizer'),
    ('16-16-16', 'Complete fertilizer'),
    ('0-20-20', 'Low-nitrogen fertilizer'),
    ('10-20-20', 'Low-nitrogen fertilizer'),
    ('Urea', 'Nitrogen fertilizer'),
    ('Ammonium Sulfate', 'Nitrogen fertilizer'),
    ('Solophos', 'Phosphorus fertilizer'),
    ('DAP', 'Phosphorus fertilizer'),
    ('Muriate of Potash', 'Potassium fertilizer'),
    ('Potassium Sulfate', 'Potassium fertilizer'),
]

CHEMICAL_PRODUCT_TYPES = [
    ('Bacillus thuringiensis', 'Biological insecticide'),
    ('Neem Oil', 'Organic insecticide'),
    ('insecticidal soap', 'Organic insecticide'),
    ('organic pest control', 'Organic pest control'),
    ('Spinosad', 'Organic insecticide'),
    ('sticky trap', 'Trap'),
    ('Epsom', 'Foliar nutrient spray'),
    ('magnesium sulfate', 'Foliar nutrient spray'),
    ('iron chelate', 'Foliar nutrient spray'),
    ('urea solution', 'Foliar nutrient spray'),
    ('phosphoric acid', 'Foliar nutrient spray'),
    ('potassium bicarbonate solution', 'Foliar nutrient spray'),
    ('nitrogen-based foliar', 'Foliar nutrient spray'),
    ('foliar spray', 'Foliar nutrient spray'),
    ('foliar application', 'Foliar nutrient spray'),
    ('Confidor', 'Insecticide'),
    ('Imidacloprid', 'Insecticide'),
    ('Actara', 'Insecticide'),
    ('Thiamethoxam', 'Insecticide'),
    ('Cymbush', 'Insecticide'),
    ('Cypermethrin', 'Insecticide'),
    ('CYPEX', 'Insecticide'),
    ('Karate', 'Insecticide'),
    ('Lambda-Cyhalothrin', 'Insecticide'),
    ('insecticide', 'Insecticide'),
    ('Daconil', 'Fungicide'),
    ('Chlorothalonil', 'Fungicide'),
    ('Dithane', 'Fungicide'),
    ('Mancozeb', 'Fungicide'),
    ('Pencozeb', 'Fungicide'),
    ('Antracol', 'Fungicide'),
    ('Ridomil', 'Fungicide'),
    ('Metalaxyl', 'Fungicide'),
    ('Copper', 'Fungicide'),
    ('Kocide', 'Fungicide'),
    ('Sulfur', 'Fungicide'),
    ('Score', 'Fungicide'),
    ('Potassium Bicarbonate', 'Fungicide'),
    ('fungicide', 'Fungicide'),
]


def _match_product_type(line, rules, default):
    lowered = (line or '').lower()
    for keyword, label in rules:
        if keyword.lower() in lowered:
            return label
    return default


def fertilizer_product_type(line):
    """'Complete fertilizer', 'Nitrogen fertilizer', … for one fertilizer recommendation."""
    return _match_product_type(line, FERTILIZER_PRODUCT_TYPES, 'Fertilizer')


def chemical_product_type(line):
    """'Fungicide', 'Insecticide', … for one chemical line; advice with no product is a practice."""
    # The disease line already opens with 'For <disease>:', so tagging it would read as two
    # colons in a row. None means "print this one as it is".
    if (line or '').startswith('For '):
        return None
    return _match_product_type(line, CHEMICAL_PRODUCT_TYPES, 'Cultural practice')


def build_recommendation_items(lines, classifier):
    """Split recommendations into {type, text} so the card can show the kind as its own label.

    Stored under 'reco_items' rather than 'items' — a template writing dict.items would call
    the dictionary's own items() method instead of reading the key.
    """
    items = []
    for line in lines:
        text = str(line).strip()
        if not text:
            continue
        items.append({'type': classifier(text), 'text': text})
    return items


def issue_description(issue):
    """How an issue reads in a sentence: a deficiency, a detected disease, or general care."""
    if not issue or issue in ('none', 'auto'):
        return 'general treatment'
    if issue == 'multiple':
        return 'multiple nutrients low'
    if issue in ('nitrogen', 'phosphorus', 'potassium', 'magnesium', 'iron'):
        return f'{issue} deficiency'
    return issue


def get_fertilizer_recommendations(plant_age, deficiency, n_level, p_level, k_level):
    """Get fertilizer recommendations based on plant age, deficiency, and NPK levels.

    Recommendation logic (Philippine market):
    - If N is low -> Urea, Ammonium Sulfate, 14-14-14
    - If P is low -> Solophos, DAP
    - If K is low -> Muriate of Potash, Potassium Sulfate
    - If N high but P/K low -> 0-20-20, 10-20-20
    - If all slightly low or balanced -> 14-14-14, 16-16-16
    """
    recs = []

    # Interpret numeric sensor values (ppm) using the same thresholds as the front-end tiles
    # and detect_npk_deficiency: below NPK_LOW_PPM is Low, above NPK_HIGH_PPM is High.
    def is_low(v):
        try:
            return float(v) < NPK_LOW_PPM
        except Exception:
            return False

    def is_high(v):
        try:
            return float(v) > NPK_HIGH_PPM
        except Exception:
            return False

    n_low = is_low(n_level)
    p_low = is_low(p_level)
    k_low = is_low(k_level)
    n_high = is_high(n_level)

    # Specific nutrient recommendations
    if n_low and not (p_low or k_low):
        recs.extend(["Use Urea (46-0-0)", "Use Ammonium Sulfate (21-0-0)", "Use 14-14-14 (Complete)"])
    if p_low and not (n_low or k_low):
        recs.extend(["Use Solophos (0-20-0)", "Use DAP (18-46-0)"])
    if k_low and not (n_low or p_low):
        recs.extend(["Use Muriate of Potash (0-0-60)", "Use Potassium Sulfate (0-0-50)"])

    # Multiple deficiencies -> combine sensible options (keep concise)
    if (n_low and p_low) and not k_low:
        recs.extend(["Use 14-14-14 (Complete)", "Use DAP (18-46-0)"])
    if (n_low and k_low) and not p_low:
        recs.extend(["Use 14-14-14 (Complete)", "Use Muriate of Potash (0-0-60)"])
    if (p_low and k_low) and not n_low:
        recs.extend(["Use 14-14-14 (Complete)", "Use Muriate of Potash (0-0-60)"])
    if n_low and p_low and k_low:
        recs.extend(["Use 14-14-14 (Complete)", "Use 16-16-16 (Complete)"])

    # Special case: N high but P and/or K low
    if n_high and (p_low or k_low):
        recs.extend(["Use 0-20-20", "Use 10-20-20"]) 

    # Slightly low/balanced overall -> suggest general complete fertilizers
    if not recs:
        # If values are all present but none low, suggest general complete fertilizer
        try:
            vals = [float(n_level), float(p_level), float(k_level)]
            avg = sum(vals) / 3
            if avg < 100:
                recs.extend(["Use 14-14-14 (Complete)", "Use 16-16-16 (Complete)"])
        except Exception:
            # Fallback: recommend a balanced fertilizer
            recs.append("Use 14-14-14 (Complete)")

    # Remove duplicates while preserving order
    seen = set()
    final = []
    for r in recs:
        if r not in seen:
            seen.add(r)
            final.append(r)

    return final

def get_chemical_recommendations(plant_age, deficiency):
    """Chemical recommendations for a plant age and an issue.

    The issue is either a nutrient deficiency chosen by hand, or the name of a disease the
    Predict tool detected — in which case the treatment comes from DISEASE_INFO, the same
    medicine list the Variety Guide and the prediction result page show.
    """
    recommendations = []

    # Age-based chemical applications
    if plant_age <= 4:  # Seedling stage
        recommendations.append("Avoid chemical applications until plants are established")
        recommendations.append("Use organic pest control if needed")
    elif plant_age <= 8:  # Vegetative growth
        recommendations.append("Apply preventive fungicide every 2-3 weeks")
        recommendations.append("Use insecticidal soap for pest control")
    elif plant_age <= 12:  # Flowering/Fruiting
        recommendations.append("Apply fungicide at first sign of disease")
        recommendations.append("Use targeted insecticides for specific pests")
    else:  # Mature plants
        recommendations.append("Apply systemic fungicides preventively")
        recommendations.append("Monitor for pests regularly")

    # Deficiency-specific chemical treatments
    if deficiency == 'nitrogen':
        recommendations.append("Apply nitrogen-based foliar spray")
        recommendations.append("Use urea solution (1 tbsp per gallon)")
    elif deficiency == 'phosphorus':
        recommendations.append("Apply phosphoric acid solution")
        recommendations.append("Use as foliar spray or soil drench")
    elif deficiency == 'potassium':
        recommendations.append("Apply potassium bicarbonate solution")
        recommendations.append("Use as foliar application")
    elif deficiency == 'magnesium':
        recommendations.append("Apply magnesium sulfate (Epsom salt)")
        recommendations.append("Use 1-2 tbsp per gallon as foliar spray")
    elif deficiency == 'iron':
        recommendations.append("Apply iron chelate solution")
        recommendations.append("Use as foliar spray or soil application")
    elif deficiency in DISEASE_INFO and deficiency != 'Healthy Eggplant Leaf':
        info = DISEASE_INFO[deficiency]
        recommendations.append(f"For {deficiency}: {info['recommendation']}")
        recommendations.extend(f"Apply {medicine}" for medicine in info['medicine'])

    return "; ".join(recommendations)

# Chart series keys mapped to every field name a device might publish them under.
ANALYTICS_FIELD_ALIASES = {
    'soil_moisture': ('soil_moisture', 'soil_moisture_percent', 'soilMoisture'),
    'temperature': ('temperature', 'temperature_c', 'temp'),
    'humidity': ('humidity', 'hum'),
    'n': ('n', 'nitrogen'),
    'p': ('p', 'phosphorus'),
    'k': ('k', 'potassium'),
    'soil_ph': ('soil_ph', 'soil_pH', 'ph'),
}

ANALYTICS_HISTORY_LIMIT = 50


def _analytics_number(value):
    """Coerce a Firestore field to a chart-friendly number, or None when it is not numeric."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _analytics_reading(payload):
    """Pull one chart point out of a sensor document, tolerating alternate field names."""
    reading = {}
    for series_key, aliases in ANALYTICS_FIELD_ALIASES.items():
        value = None
        for alias in aliases:
            if alias in payload:
                value = _analytics_number(payload[alias])
                if value is not None:
                    break
        reading[series_key] = value
    return reading


def _collect_analytics_docs(client, user_id, season_id):
    """Every sensor document that could belong to this user, from all the places they are written.

    Mirrors the lookup get_sensor_data performs: the season subcollection, the user-level
    collection, and the shared top-level collection the Raspberry Pi publishes to. Charting only
    the season subcollection is why history looked empty while the live tiles showed readings.
    """
    collected = {}

    def _absorb(docs):
        for doc in docs or []:
            try:
                collected[doc.reference.path] = doc
            except Exception:
                continue

    def _recent(collection):
        try:
            return collection.order_by('timestamp', direction=firestore.Query.DESCENDING).limit(ANALYTICS_HISTORY_LIMIT).get()
        except Exception:
            # Devices that write no 'timestamp' field make the ordered query fail; read them all.
            return collection.get()

    if season_id:
        season_ref = client.collection('users').document(user_id).collection('seasons').document(season_id)
        try:
            _absorb(_recent(season_ref.collection('sensor_data')))
        except Exception as query_error:
            print(f"[get_analytics_data] season sensor_data query failed: {query_error}")

    try:
        _absorb(_recent(client.collection('users').document(user_id).collection('sensor_data')))
    except Exception as query_error:
        print(f"[get_analytics_data] user-level sensor_data query failed: {query_error}")

    try:
        _absorb(client.collection('sensor_data').get())
    except Exception as query_error:
        print(f"[get_analytics_data] shared sensor_data query failed: {query_error}")

    return list(collected.values())


def get_analytics_data(request):
    """Return recent sensor history as chart-friendly series for active season analytics."""
    if not request.user or not request.user.is_authenticated:
        return JsonResponse({'error': 'Authentication required'}, status=401)

    user_id = str(request.user.id)
    client = get_firestore_client()
    if client is None:
        return JsonResponse({'error': 'Firebase not configured'}, status=500)

    empty_series = {key: [] for key in ANALYTICS_FIELD_ALIASES}

    active_season = get_active_season(user_id)
    season_id = active_season.get('id') if active_season else None

    docs = _collect_analytics_docs(client, user_id, season_id)

    sensor_history = []
    for doc in docs:
        payload = doc.to_dict() or {}
        # Skip the pump-status stubs this app writes into the same collection; they carry a
        # timestamp but no measurement, and plotting them produced a chart of empty points.
        if not has_sensor_readings(payload):
            continue
        # document_written_at falls back to Firestore's update_time, so the shared
        # sensor_data/latest document (which stores no timestamp field) still gets charted.
        ts = document_written_at(doc)
        if not isinstance(ts, datetime) or ts == datetime.min:
            continue
        entry = _analytics_reading(payload)
        if all(value is None for value in entry.values()):
            continue
        entry['timestamp'] = ts
        entry['sort_key'] = document_write_sort_key(doc)
        sensor_history.append(entry)

    sensor_history.sort(key=lambda item: item['sort_key'])
    sensor_history = sensor_history[-ANALYTICS_HISTORY_LIMIT:]

    if not sensor_history:
        return JsonResponse({'labels': [], 'series': empty_series})

    labels = [item['timestamp'].strftime('%m-%d %H:%M') for item in sensor_history]
    series = {
        key: [item[key] for item in sensor_history]
        for key in ANALYTICS_FIELD_ALIASES
    }

    return JsonResponse({'labels': labels, 'series': series})


def current_sensor_feed(user_id):
    from .sensor_feed import read_feed, offline_feed
    try:
        client = get_firestore_client()
        if client is None:
            return offline_feed('Sensor service unavailable.')
        season = get_active_season(user_id)
        return read_feed(client, user_id, season.get('id') if season else None,
                         RASPBERRY_PI_OFFLINE_AFTER_SECONDS)
    except Exception:
        logging.exception('Could not retrieve live sensor feed')
        return offline_feed('Could not retrieve live sensor data.')


def get_sensor_data(request):
    if not request.user or not request.user.is_authenticated:
        return JsonResponse({'error': 'Authentication required'}, status=401)
    data = current_sensor_feed(str(request.user.id))
    try:
        spray_active = get_spray_schedule(str(request.user.id))['active']
    except Exception:
        spray_active = None
    data.update(
        water_pump_active=manual_watering.get('active', False),
        spray_pump_active=manual_spraying.get('active', False),
        water_schedule_active=watering_schedule.get('active', False),
        spray_schedule_active=spray_active,
    )
    response = JsonResponse(data)
    response['Cache-Control'] = 'no-store'
    return response


def get_weather(request):
    """
    Simple proxy endpoint to fetch weather data. The client may either provide a full
    weather API URL via the `url` query parameter, or the project can set
    `settings.WEATHER_API_URL` (not required here). This endpoint will GET the
    external URL and return whatever JSON the provider returns.

    NOTE: This is a lightweight helper for development. In production, configure
    a proper server-side integration and secure the API key.
    """
    # Optional override URL. If not provided, default to OpenWeather for Naujan, Oriental Mindoro.
    url = request.GET.get('url')
    if not url:
        # Accept key from query string first, then environment variable.
        api_key = request.GET.get('api_key') or os.environ.get('OPENWEATHER_API_KEY')
        if not api_key:
            return JsonResponse({'error': 'Missing weather API key. Provide ?api_key=... or set OPENWEATHER_API_KEY.'}, status=400)

        # Naujan, Oriental Mindoro, PH (OpenWeather Current Weather API)
        query = urllib.parse.quote('Naujan,Oriental Mindoro,PH')
        url = f'https://api.openweathermap.org/data/2.5/weather?q={query}&appid={api_key}&units=metric'

    try:
        resp = requests.get(url, timeout=6)
        # Try to return JSON as-is; preserve upstream status code for easier debugging.
        try:
            return JsonResponse(resp.json(), status=resp.status_code)
        except ValueError:
            # Not JSON — return text wrapper
            return JsonResponse({'raw': resp.text}, status=resp.status_code)
    except Exception as e:
        print(f"Error proxying weather URL: {e}")
        return JsonResponse({'error': 'Could not fetch weather data.'}, status=500)


@login_required
@require_POST
def create_rtmp_stream(request):
    """
    Generate an RTMP base + secure stream key for the logged-in user.

    The key is registered as a `streams.Stream` row (not just stored in Firestore) because
    MediaMTX is configured with HTTP auth (see deployment/mediamtx.yml) that validates every
    publish attempt against that table via MediaMTXAuthAPIView. A key that only exists in
    Firestore is rejected by MediaMTX, so the drone can never actually start publishing.

    Returns JSON: { ok: True, stream_key, rtmp_base, publish_url, hls_url }
    """
    try:
        # One Stream per predictions user, identified by a stable naming convention (the Stream
        # model has no user FK — it's a shared registry used by the admin-facing streams app too).
        stream_name = f'predictions:{request.user.id}'
        stream, created = Stream.objects.get_or_create(
            stream_name=stream_name,
            defaults={'is_active': True},
        )
        if not created:
            stream.regenerate_stream_key()
            if not stream.is_active:
                stream.is_active = True
                stream.save(update_fields=['is_active', 'updated_at'])

        payload = {
            'stream_key': stream.stream_key,
            'rtmp_base': stream.rtmp_server_url,
            'publish_url': stream.rtmp_publish_url,
            'hls_url': stream.hls_playback_url,
        }

        # Also persist to Firestore so the mapping is available elsewhere (Pi, admin UI, etc.)
        client = get_firestore_client()
        if client is not None:
            try:
                client.collection('users').document(str(request.user.id)).collection('live_streams').add({
                    **payload,
                    'active': True,
                    'created_at': timezone.now().isoformat(),
                })
            except Exception:
                logging.exception('Failed to persist RTMP stream credentials to Firestore')

        try:
            ActivityHistory.objects.create(user=request.user, action='create_rtmp_stream', details=payload['publish_url'])
        except Exception:
            logging.exception('Failed to log ActivityHistory for create_rtmp_stream')

        return JsonResponse({'ok': True, **payload})
    except Exception as e:
        logging.exception('Failed to create RTMP stream credentials')
        return JsonResponse({'ok': False, 'error': str(e)}, status=500)


@login_required
def live_stream_status(request):
    """
    Report the drone stream's real state, backed by MediaMTX's publish table rather than a
    client-side guess. LIVE is only ever reported when MediaMTX confirms the path is ready.

    Returns 200 with a `state` the UI can render directly:
      no_credentials  - user has not generated a stream key yet
      server_offline  - MediaMTX is not reachable, so liveness is unknown (never LIVE)
      stopped         - the key is revoked; the drone cannot publish until Start Scan
      waiting         - key is armed, MediaMTX is up, drone is not publishing yet
      live            - MediaMTX confirms the drone is publishing
    """
    stream_name = f'predictions:{request.user.id}'
    stream = Stream.objects.filter(stream_name=stream_name).first()
    if not stream:
        return JsonResponse({
            'ok': True,
            'state': 'no_credentials',
            'is_live': False,
            'mediamtx_online': None,
            'hls_url': None,
            'message': 'Create credentials to get an RTMP address for the drone.',
        })

    try:
        stream = sync_stream(stream)
    except RuntimeError as exc:
        # MediaMTX is down, so nothing can confirm a publisher. Report unknown rather than LIVE.
        return JsonResponse({
            'ok': True,
            'state': 'server_offline',
            'is_live': False,
            'mediamtx_online': False,
            'hls_url': None,
            'message': f'Streaming server unreachable: {exc}',
        })

    if not stream.is_active:
        state = 'stopped'
        message = 'Scan stopped. Press Start Scan before publishing from the drone.'
    elif stream.is_live:
        state = 'live'
        message = 'Drone is publishing.'
    else:
        state = 'waiting'
        message = 'Waiting for drone...'

    return JsonResponse({
        'ok': True,
        'state': state,
        'is_live': stream.is_live,
        'mediamtx_online': True,
        'hls_url': stream.hls_url_for_path(getattr(stream, 'live_path_name', '')) if stream.is_live else None,
        'message': message,
    })


@login_required
@require_POST
def start_drone_scan(request):
    """Arm the user's stream key so the drone is allowed to publish.

    The drone pushes RTMP straight into MediaMTX; the server never spawns ffmpeg for it. So
    "Start Scan" means re-enabling the key that MediaMTXAuthAPIView checks on every publish
    attempt, not launching an encoder.
    """
    stream_name = f'predictions:{request.user.id}'
    stream = Stream.objects.filter(stream_name=stream_name).first()
    if not stream:
        return JsonResponse({
            'ok': False,
            'error': 'Create credentials first.',
            'code': 'no_credentials',
        }, status=400)

    if not stream.is_active:
        stream.is_active = True
        stream.save(update_fields=['is_active', 'updated_at'])
        StreamLog.objects.create(stream=stream, event=StreamLog.Event.ENABLED)

    try:
        ActivityHistory.objects.create(
            user=request.user, action='start_live_stream', details=f'drone_rtmp:{stream.rtmp_publish_url}')
    except Exception:
        logging.exception('Failed to log ActivityHistory for start_drone_scan')

    return JsonResponse({
        'ok': True,
        'rtmp_base': stream.rtmp_server_url,
        'stream_key': stream.stream_key,
        'publish_url': stream.rtmp_publish_url,
        'message': 'Ready. Start the RTMP broadcast in DJI Fly.',
    })


@login_required
@require_POST
def capture_live_frame(request):
    """Run disease detection on a single frame grabbed from the live drone feed.

    The browser draws the current <video> frame to a canvas and posts it as a JPEG, so this is
    the same job as the upload flow - it deliberately reuses predict_plant_disease and
    record_disease_detection rather than re-implementing them, so a capture and an upload of the
    same leaf always produce the same verdict and the same Variety Guide bookkeeping.

    Returns JSON: { ok, predicted_class, confidence, recommendation, image_url, disease_info }
    """
    frame = request.FILES.get('frame')
    if not frame:
        return JsonResponse({'ok': False, 'error': 'No frame was received.'}, status=400)

    # Frames come straight off a 720p canvas; anything much larger is not a video still.
    max_bytes = 12 * 1024 * 1024
    if frame.size > max_bytes:
        return JsonResponse({'ok': False, 'error': 'Captured frame is too large.'}, status=400)

    media_dir = os.path.join(settings.MEDIA_ROOT, 'live_captures')
    os.makedirs(media_dir, exist_ok=True)

    filename = f"live_{request.user.id}_{timezone.now():%Y%m%d_%H%M%S}_{secrets.token_hex(4)}.jpg"
    fs = FileSystemStorage(location=media_dir, base_url=f"{settings.MEDIA_URL}live_captures/")
    saved_name = fs.save(filename, frame)
    image_path = os.path.join(media_dir, saved_name)

    try:
        predicted_class, confidence = predict_plant_disease(image_path)
    except Exception as exc:
        logging.exception('Live frame prediction failed')
        return JsonResponse({'ok': False, 'error': f'Could not analyze the frame: {exc}'}, status=500)

    try:
        ActivityHistory.objects.create(
            user=request.user,
            action='live_capture_predict',
            details=f'Predicted: {predicted_class}, Confidence: {confidence}%')
    except Exception:
        logging.exception('Failed to log ActivityHistory for capture_live_frame')

    # Same season bookkeeping as an uploaded image, so live scans tick off the Variety Guide too.
    user_id = str(request.user.id)
    active_season = get_active_season(user_id)
    if active_season:
        try:
            record_disease_detection(user_id, active_season['id'], predicted_class, confidence)
        except Exception:
            logging.exception('Failed to record live capture detection on the active season')

    healthy = predicted_class == 'Healthy Eggplant Leaf'
    info = DISEASE_INFO.get(predicted_class) or {}

    # Same alert as the upload flow, tagged as coming from the drone feed.
    notify_disease_detection(request.user, predicted_class, confidence, disease_info=info, source='live')

    return JsonResponse({
        'ok': True,
        'predicted_class': predicted_class,
        'confidence': confidence,
        'healthy': healthy,
        'recommendation': ('No action needed. Plant is healthy.' if healthy
                           else 'Disease detected. Consider treatment or removal.'),
        'about': info.get('about', ''),
        'image_url': fs.url(saved_name),
        'captured_at': timezone.localtime().strftime('%b %d, %Y %I:%M:%S %p'),
    })


@login_required
@require_POST
def stop_drone_scan(request):
    """Revoke the stream key and disconnect any drone currently publishing.

    Revoking alone is not enough: MediaMTX authenticates once at connect time, so a drone that
    is already streaming would keep going. kick_publisher drops the live connection too, and is
    best-effort so Stop still succeeds when MediaMTX is down.
    """
    stream_name = f'predictions:{request.user.id}'
    stream = Stream.objects.filter(stream_name=stream_name).first()
    if not stream:
        return JsonResponse({
            'ok': False,
            'error': 'No stream credentials to stop.',
            'code': 'no_credentials',
        }, status=400)

    if stream.is_active:
        stream.is_active = False
        stream.save(update_fields=['is_active', 'updated_at'])
        StreamLog.objects.create(stream=stream, event=StreamLog.Event.DISABLED)

    kicked = kick_publisher(stream.stream_key)

    if stream.is_live:
        stream.is_live = False
        stream.live_started_at = None
        stream.save(update_fields=['is_live', 'live_started_at', 'updated_at'])
        StreamLog.objects.create(stream=stream, event=StreamLog.Event.LIVE_STOPPED)

    try:
        ActivityHistory.objects.create(user=request.user, action='stop_live_stream', details='drone_rtmp:stopped')
    except Exception:
        logging.exception('Failed to log ActivityHistory for stop_drone_scan')

    return JsonResponse({
        'ok': True,
        'disconnected': kicked,
        'message': 'Scan stopped and the stream key was revoked.',
    })


@login_required
def notification_settings(request):
    """Let the farmer choose where alerts go: Gmail inbox, SMS, or both."""
    preference, _ = NotificationPreference.objects.get_or_create(user=request.user)

    if request.method == 'POST':
        email = (request.POST.get('email') or '').strip()
        raw_phone = (request.POST.get('phone_number') or '').strip()

        # An SMS channel with an unusable number would fail silently at send time, so the
        # number is validated here - while the user is looking at the form - not later.
        normalized_phone = normalize_phone_number(raw_phone) if raw_phone else ''
        if raw_phone and not normalized_phone:
            messages.error(request, 'That mobile number does not look valid. Use 09171234567 or +639171234567.')
            return redirect('notification_settings')

        wants_sms = request.POST.get('sms_enabled') == 'on'
        if wants_sms and not normalized_phone:
            messages.error(request, 'Add a mobile number before turning on SMS alerts.')
            return redirect('notification_settings')

        wants_email = request.POST.get('email_enabled') == 'on'
        if wants_email and not email:
            messages.error(request, 'Add an e-mail address before turning on e-mail alerts.')
            return redirect('notification_settings')

        if email != request.user.email:
            request.user.email = email
            request.user.save(update_fields=['email'])

        preference.phone_number = normalized_phone
        preference.email_enabled = wants_email
        preference.sms_enabled = wants_sms
        preference.web_enabled = request.POST.get('web_enabled') == 'on'
        preference.notify_on_detection = request.POST.get('notify_on_detection') == 'on'
        preference.notify_healthy_results = request.POST.get('notify_healthy_results') == 'on'
        preference.notify_on_schedule = request.POST.get('notify_on_schedule') == 'on'
        preference.save()

        messages.success(request, 'Notification settings saved.')
        return redirect('notification_settings')

    return render(request, 'notification_settings.html', {
        'preference': preference,
        'email_configured': email_is_configured(),
        'sms_configured': sms_is_configured(),
        'sms_provider': sms_provider(),
    })


@login_required
@require_POST
def send_test_notification(request):
    """Send a test alert on the channels that are switched on and report what happened.

    This one is deliberately synchronous: the whole point is to show the user the
    gateway's answer, so it waits for the send instead of dispatching to a thread.
    """
    preference, _ = NotificationPreference.objects.get_or_create(user=request.user)

    if not preference.email_enabled and not preference.sms_enabled and not preference.web_enabled:
        messages.error(request, 'Turn on at least one channel first, then send a test.')
        return redirect('notification_settings')

    subject = 'Hi-Tech Eggplant test notification'
    body = '\n'.join([
        f'Hello {request.user.first_name or request.user.username},',
        '',
        'This is a test alert from your Hi-Tech Eggplant dashboard.',
        'If you received it, disease detections and scheduled applications will reach you here too.',
        '',
        f'Sent {timezone.localtime():%b %d, %Y %I:%M %p}.',
    ])
    sms_body = 'HI-TECH EGGPLANT: test alert. Your SMS notifications are working.'

    results = notify_user(
        request.user, subject, body, sms_message=sms_body,
        category='test', level='info', link_url='/notifications/')

    for channel, outcome in results.items():
        if outcome is None:
            continue
        sent, detail = outcome
        if sent:
            messages.success(request, f'{channel.upper()}: {detail}')
        else:
            messages.error(request, f'{channel.upper()}: {detail}')

    try:
        ActivityHistory.objects.create(
            user=request.user,
            action='test_notification',
            details=str({channel: outcome for channel, outcome in results.items() if outcome}),
        )
    except Exception:
        logging.exception('Failed to log ActivityHistory for send_test_notification')

    return redirect('notification_settings')


NOTIFICATION_FEED_LIMIT = 10


def _serialize_notification(notification):
    """Shape one notification for the bell panel and the inbox page."""
    created = timezone.localtime(notification.created_at)
    return {
        'id': notification.id,
        'title': notification.title,
        'message': notification.message,
        'category': notification.category,
        'level': notification.level,
        'link_url': notification.link_url,
        'is_read': notification.is_read,
        'created_at': created.isoformat(),
        'created_display': created.strftime('%b %d, %Y %I:%M %p'),
        'age': _humanize_age(created),
    }


def _humanize_age(moment):
    """A short "3m ago" style age, so the panel does not need a date library."""
    seconds = int((timezone.now() - moment).total_seconds())
    if seconds < 60:
        return 'just now'
    if seconds < 3600:
        return f'{seconds // 60}m ago'
    if seconds < 86400:
        return f'{seconds // 3600}h ago'
    if seconds < 604800:
        return f'{seconds // 86400}d ago'
    return moment.strftime('%b %d')


@login_required
def notification_feed(request):
    """JSON feed polled by the bell in the sidebar.

    `since` (an id) lets the poller ask "anything newer than what I already show?",
    which is what drives the desktop pop-up without re-rendering the whole list.
    """
    queryset = Notification.objects.filter(user=request.user)

    try:
        limit = min(int(request.GET.get('limit', NOTIFICATION_FEED_LIMIT)), 50)
    except (TypeError, ValueError):
        limit = NOTIFICATION_FEED_LIMIT

    if request.GET.get('unread_only') == '1':
        queryset = queryset.filter(is_read=False)

    notifications = list(queryset[:limit])

    fresh = []
    since = request.GET.get('since')
    if since:
        try:
            fresh = [n for n in notifications if n.id > int(since)]
        except (TypeError, ValueError):
            fresh = []

    return JsonResponse({
        'ok': True,
        'unread_count': Notification.objects.filter(user=request.user, is_read=False).count(),
        'latest_id': notifications[0].id if notifications else 0,
        'notifications': [_serialize_notification(n) for n in notifications],
        'new': [_serialize_notification(n) for n in fresh],
    })


@login_required
@require_POST
def mark_notifications_read(request):
    """Mark one notification read, or all of them when no id is given."""
    notification_id = request.POST.get('id')
    queryset = Notification.objects.filter(user=request.user, is_read=False)

    if notification_id:
        queryset = queryset.filter(id=notification_id)

    updated = queryset.update(is_read=True, read_at=timezone.now())

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({
            'ok': True,
            'updated': updated,
            'unread_count': Notification.objects.filter(user=request.user, is_read=False).count(),
        })

    return redirect('notification_inbox')


@login_required
def notification_inbox(request):
    """Full history of everything the system has told this user."""
    notifications = Notification.objects.filter(user=request.user)[:100]
    return render(request, 'notification_inbox.html', {
        'notifications': notifications,
        'unread_count': Notification.objects.filter(user=request.user, is_read=False).count(),
    })


@require_POST
def google_login(request):
    return JsonResponse({"ok": False, "error": "Google sign-in is disabled. Use the system account."}, status=403)
