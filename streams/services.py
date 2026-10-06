import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from django.conf import settings
from django.utils import timezone

from .models import Stream, StreamLog, extract_stream_key


def fetch_live_stream_keys() -> set[str]:
    endpoint = f"{settings.MEDIAMTX_API_BASE_URL.rstrip('/')}/v3/paths/list"
    request = Request(endpoint, method="GET")

    try:
        with urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise RuntimeError(f"MediaMTX API returned HTTP {exc.code}") from exc
    except URLError as exc:
        raise RuntimeError("Unable to connect to MediaMTX API.") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError("MediaMTX API returned invalid JSON.") from exc

    items = payload.get("items")
    if not isinstance(items, list):
        raise RuntimeError("MediaMTX API payload is missing expected items list.")

    return set(fetch_live_paths_from_items(items))


def fetch_live_paths_from_items(items) -> dict[str, str]:
    """Map each publishing stream key to the MediaMTX path name it is actually published under.

    The path matters, not just the key: HLS is served at /<path>/index.m3u8, and a drone that
    joined the URL without a "/" publishes to "livedji_<key>" instead of "live/<key>". Playback
    has to point at whichever one MediaMTX really created.
    """
    live_paths: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        if not item.get("ready"):
            continue
        path_name = str(item.get("name", ""))
        key = extract_stream_key(path_name)
        if key:
            live_paths[key] = path_name
    return live_paths


def fetch_live_paths() -> dict[str, str]:
    """{stream_key: mediamtx_path_name} for every path currently being published."""
    payload = _mediamtx_request("/v3/paths/list")
    items = (payload or {}).get("items")
    if not isinstance(items, list):
        raise RuntimeError("MediaMTX API payload is missing expected items list.")
    return fetch_live_paths_from_items(items)


def sync_stream_states() -> list[Stream]:
    live_keys = fetch_live_stream_keys()
    now = timezone.now()
    changed_streams: list[Stream] = []

    for stream in Stream.objects.select_related("drone", "mission").all():
        should_be_live = stream.is_active and stream.stream_key in live_keys
        if should_be_live == stream.is_live:
            if should_be_live:
                stream.last_activity_at = now
                stream.save(update_fields=["last_activity_at", "updated_at"])
            continue

        stream.is_live = should_be_live
        if should_be_live:
            stream.live_started_at = now
            stream.last_activity_at = now
            stream.save(update_fields=["is_live", "live_started_at", "last_activity_at", "updated_at"])
            StreamLog.objects.create(stream=stream, event=StreamLog.Event.LIVE_STARTED)
        else:
            stream.live_started_at = None
            stream.last_activity_at = now
            stream.save(update_fields=["is_live", "live_started_at", "last_activity_at", "updated_at"])
            StreamLog.objects.create(stream=stream, event=StreamLog.Event.LIVE_STOPPED)

        changed_streams.append(stream)

    return changed_streams


def _mediamtx_request(path: str, method: str = "GET", timeout: int = 5):
    """Call the MediaMTX control API, returning parsed JSON (or None for empty bodies)."""
    endpoint = f"{settings.MEDIAMTX_API_BASE_URL.rstrip('/')}{path}"
    request = Request(endpoint, method=method)

    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8").strip()
    except HTTPError as exc:
        raise RuntimeError(f"MediaMTX API returned HTTP {exc.code}") from exc
    except URLError as exc:
        raise RuntimeError("Unable to connect to MediaMTX API.") from exc

    if not body:
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError("MediaMTX API returned invalid JSON.") from exc


def sync_stream(stream: Stream) -> Stream:
    """Refresh one stream's live flag from MediaMTX.

    sync_stream_states() walks and writes every row in the table, which is far too much work for
    a dashboard that polls every few seconds. This touches only the stream being watched, but
    keeps the same rule: live means MediaMTX reports the path as ready, never a client guess.

    Raises RuntimeError when MediaMTX is unreachable, so callers can tell "streaming server is
    down" apart from "drone has not started publishing yet".
    """
    live_paths = fetch_live_paths()
    now = timezone.now()
    should_be_live = stream.is_active and stream.stream_key in live_paths
    # Not persisted: the caller needs the real path only for the URL it hands back right now.
    stream.live_path_name = live_paths.get(stream.stream_key, "")

    if should_be_live == stream.is_live:
        stream.last_activity_at = now
        stream.save(update_fields=["last_activity_at", "updated_at"])
        return stream

    stream.is_live = should_be_live
    stream.live_started_at = now if should_be_live else None
    stream.last_activity_at = now
    stream.save(update_fields=["is_live", "live_started_at", "last_activity_at", "updated_at"])
    StreamLog.objects.create(
        stream=stream,
        event=StreamLog.Event.LIVE_STARTED if should_be_live else StreamLog.Event.LIVE_STOPPED,
    )
    return stream


def kick_publisher(stream_key: str) -> bool:
    """Best-effort disconnect of whatever is currently publishing to this stream key.

    Revoking the key only stops the *next* publish attempt — MediaMTX authenticates once, at
    connect time, so a drone already streaming keeps going until it is kicked. Returns True when
    a connection was actually dropped. Never raises: Stop must still succeed with MediaMTX down.
    """
    try:
        payload = _mediamtx_request("/v3/rtmpconns/list")
    except RuntimeError:
        return False

    items = (payload or {}).get("items")
    if not isinstance(items, list):
        return False

    kicked = False
    for item in items:
        if not isinstance(item, dict):
            continue
        if str(item.get("path", "")).split("/")[-1] != stream_key:
            continue
        if str(item.get("state", "")) != "publish":
            continue
        connection_id = item.get("id")
        if not connection_id:
            continue
        try:
            _mediamtx_request(f"/v3/rtmpconns/kick/{connection_id}", method="POST")
            kicked = True
        except RuntimeError:
            continue
    return kicked
