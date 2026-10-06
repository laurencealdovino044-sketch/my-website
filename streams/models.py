import re
import secrets
import string
from datetime import timedelta

from django.conf import settings
from django.core.validators import RegexValidator
from django.db import models
from django.utils import timezone


STREAM_KEY_ALPHABET = string.ascii_letters + string.digits
STREAM_KEY_PREFIX = "dji_"
STREAM_KEY_LENGTH = 32


def generate_stream_key_value() -> str:
    return f"{STREAM_KEY_PREFIX}{''.join(secrets.choice(STREAM_KEY_ALPHABET) for _ in range(STREAM_KEY_LENGTH))}"


# Matches a stream key anywhere in a MediaMTX path name. Splitting on "/" is not enough:
# DJI Fly concatenates its RTMP server and stream-key fields without a separator, so the path
# arrives as "livedji_<key>" rather than "live/dji_<key>". The key format is strict enough
# (dji_ + exactly 32 alphanumerics) that finding it in the path stays unambiguous.
STREAM_KEY_IN_PATH_RE = re.compile(rf"{STREAM_KEY_PREFIX}[A-Za-z0-9]{{{STREAM_KEY_LENGTH}}}")


def extract_stream_key(path_name: str) -> str:
    """Pull the stream key out of a MediaMTX path name, with or without the '/' separator."""
    if not path_name:
        return ""
    match = STREAM_KEY_IN_PATH_RE.search(path_name)
    return match.group(0) if match else ""


class Drone(models.Model):
    drone_name = models.CharField(max_length=120)
    serial_number = models.CharField(max_length=120, unique=True)
    model = models.CharField(max_length=120)

    class Meta:
        ordering = ("drone_name",)

    def __str__(self) -> str:
        return f"{self.drone_name} ({self.serial_number})"


class Mission(models.Model):
    mission_name = models.CharField(max_length=120)
    location = models.CharField(max_length=255)
    description = models.TextField(blank=True)

    class Meta:
        ordering = ("-id",)

    def __str__(self) -> str:
        return self.mission_name


class Stream(models.Model):
    stream_name = models.CharField(max_length=120)
    drone = models.ForeignKey(Drone, on_delete=models.SET_NULL, null=True, blank=True, related_name="streams")
    mission = models.ForeignKey(Mission, on_delete=models.SET_NULL, null=True, blank=True, related_name="streams")
    stream_key = models.CharField(
        max_length=64,
        unique=True,
        validators=[
            RegexValidator(
                regex=r"^dji_[A-Za-z0-9]{32}$",
                message="Stream key must match dji_<32 alpha-numeric chars>.",
            )
        ],
    )
    is_active = models.BooleanField(default=True)
    is_live = models.BooleanField(default=False)
    live_started_at = models.DateTimeField(null=True, blank=True)
    last_activity_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self) -> str:
        return self.stream_name

    @classmethod
    def generate_unique_stream_key(cls) -> str:
        for _ in range(30):
            candidate = generate_stream_key_value()
            if not cls.objects.filter(stream_key=candidate).exists():
                return candidate
        raise RuntimeError("Unable to generate a unique stream key after multiple attempts.")

    @property
    def rtmp_server_url(self) -> str:
        return settings.STREAM_RTMP_BASE_URL.rstrip("/")

    @property
    def rtmp_publish_url(self) -> str:
        return f"{self.rtmp_server_url}/{self.stream_key}"

    @property
    def hls_playback_url(self) -> str:
        return f"{settings.STREAM_HLS_BASE_URL.rstrip('/')}/{self.stream_key}/index.m3u8"

    def hls_url_for_path(self, path_name: str) -> str:
        """Playback URL for the path MediaMTX actually created for this stream.

        Falls back to the canonical live/<key> URL when no path is known.
        """
        if not path_name:
            return self.hls_playback_url
        return f"{settings.STREAM_HLS_ROOT_URL.rstrip('/')}/{path_name.strip('/')}/index.m3u8"

    @property
    def stream_duration(self) -> timedelta:
        if not self.is_live or not self.live_started_at:
            return timedelta(0)
        return timezone.now() - self.live_started_at

    def regenerate_stream_key(self) -> str:
        self.stream_key = self.generate_unique_stream_key()
        self.save(update_fields=["stream_key", "updated_at"])
        return self.stream_key

    def save(self, *args, **kwargs):
        if not self.stream_key:
            self.stream_key = self.generate_unique_stream_key()
        super().save(*args, **kwargs)


class StreamLog(models.Model):
    class Event(models.TextChoices):
        CREATED = "created", "Created"
        UPDATED = "updated", "Updated"
        ENABLED = "enabled", "Enabled"
        DISABLED = "disabled", "Disabled"
        KEY_REGENERATED = "key_regenerated", "Key Regenerated"
        LIVE_STARTED = "live_started", "Live Started"
        LIVE_STOPPED = "live_stopped", "Live Stopped"

    stream = models.ForeignKey(Stream, on_delete=models.CASCADE, related_name="logs")
    event = models.CharField(max_length=32, choices=Event.choices)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-timestamp",)

    def __str__(self) -> str:
        return f"{self.stream.stream_name} - {self.event} @ {self.timestamp:%Y-%m-%d %H:%M:%S}"
