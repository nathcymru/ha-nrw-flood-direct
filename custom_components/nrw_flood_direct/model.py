"""Strict whole-feed validation and explicit area selection; no inferred expiry."""

import math
import re
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime

from .const import MAX_FEATURES, SEVERITIES, STALE_SECONDS

AREA_CODE = re.compile(r"[A-Za-z0-9_-]{1,100}\Z")


class DataError(ValueError):
    """Invalid source data; never interpreted as an empty warning set."""


def timestamp(value):
    # The authenticated NRW v3 feed uses Unix milliseconds, including fractional
    # seconds. Do not guess seconds or the host timezone from a numeric value.
    if type(value) in (int, float):
        if not math.isfinite(value) or not 946684800000 <= value <= 4102444800000:
            raise DataError("Invalid NRW millisecond timestamp")
        return datetime.fromtimestamp(value / 1000, UTC)
    if not isinstance(value, str) or len(value) > 100:
        raise DataError("Invalid warning timestamp")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise DataError("Invalid warning timestamp") from None
    if result.tzinfo is None or result.utcoffset() is None:
        raise DataError("Warning timestamp has no verified timezone")
    return result.astimezone(UTC)


def text(value, name, maximum, *, empty=False):
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip()):
        raise DataError("Invalid " + name)
    return value.strip()


def scope(config):
    mode = config.get("mode")
    codes = config.get("area_codes", [])
    if not isinstance(codes, list) or len(codes) > MAX_FEATURES:
        raise DataError("Invalid configured flood areas")
    if any(not isinstance(code, str) or not AREA_CODE.fullmatch(code) for code in codes):
        raise DataError("Invalid configured flood area code")
    if mode == "all_wales" and not codes:
        return {"type": mode, "label": "All Wales", "area_codes": []}
    if mode == "home" and not codes:
        return {"type": "area_codes", "label": "Home flood areas", "area_codes": []}
    if mode != "area_codes" or not codes or len(set(codes)) != len(codes):
        raise DataError("Select all_wales or an explicit nonempty area_codes list")
    label = text(config.get("area_label", "Selected flood areas"), "area label", 150)
    return {"type": mode, "label": label, "area_codes": sorted(codes)}


def source_time(headers, now):
    """Bound HTTP response age; individual warning dates are not feed expiry dates."""
    try:
        raw = headers.get("Date") or headers.get("date")
        value = parsedate_to_datetime(raw)
        if value.tzinfo is None:
            raise ValueError
        value = value.astimezone(UTC)
        age_raw = headers.get("Age", headers.get("age", "0"))
        if not isinstance(age_raw, str) or not re.fullmatch(r"\d{1,10}", age_raw):
            raise ValueError
        age = int(age_raw)
    except TypeError, ValueError, OverflowError, AttributeError:
        raise DataError("Response freshness metadata is missing or invalid") from None
    # Both Date and Age must be fresh; neither can make the other younger.
    if value > now + timedelta(minutes=5) or age >= STALE_SECONDS:
        raise DataError("Response freshness metadata is outside its permitted range")
    effective = min(value, now - timedelta(seconds=age))
    if now - effective >= timedelta(seconds=STALE_SECONDS):
        raise DataError("Provider response is stale")
    return effective.isoformat()


def _pagination(payload, count):
    if any(payload.get(key) for key in ("next", "nextLink", "next_link", "continuationToken")):
        raise DataError("Paginated warning data is unsupported")
    links = payload.get("links", [])
    if not isinstance(links, list) or any(
        not isinstance(link, dict) or link.get("rel") == "next" for link in links
    ):
        raise DataError("Unverified warning pagination")
    for key in ("numberMatched", "numberReturned", "totalFeatures"):
        if key in payload and (type(payload[key]) is not int or payload[key] != count):
            raise DataError("Warning response is incomplete")


def _geometry(value):
    if value is None:
        return
    if not isinstance(value, dict) or value.get("type") != "Point":
        raise DataError("Unexpected warning geometry")
    coordinates = value.get("coordinates")
    if not isinstance(coordinates, list) or len(coordinates) not in (2, 3):
        raise DataError("Invalid warning point")
    if any(type(x) not in (float, int) or not math.isfinite(x) for x in coordinates):
        raise DataError("Invalid warning coordinates")
    if not -180 <= coordinates[0] <= 180 or not -90 <= coordinates[1] <= 90:
        raise DataError("Warning point is out of bounds")


def normalize(payload, now):
    if not isinstance(payload, dict) or payload.get("type") != "FeatureCollection":
        raise DataError("Expected a complete GeoJSON FeatureCollection")
    if any(payload.get(key) for key in ("error", "errors", "exception")):
        raise DataError("Warning response reports a provider error")
    crs = payload.get("crs")
    if crs is not None and crs != {"type": "name", "properties": {"name": "EPSG:4326"}}:
        raise DataError("Unverified warning coordinate system")
    features = payload.get("features")
    if not isinstance(features, list) or len(features) > MAX_FEATURES:
        raise DataError("Invalid warning feature list")
    _pagination(payload, len(features))
    latest = {}
    for feature in features:
        if not isinstance(feature, dict) or feature.get("type") != "Feature":
            raise DataError("Invalid warning feature")
        _geometry(feature.get("geometry"))
        properties = feature.get("properties")
        if not isinstance(properties, dict):
            raise DataError("Warning properties are missing")
        code = properties.get("FWACODE")
        if not isinstance(code, str) or not AREA_CODE.fullmatch(code):
            raise DataError("Invalid warning area code")
        level = properties.get("SEVERITYVALUE")
        if type(level) not in (int, float) or level not in SEVERITIES:
            raise DataError("Invalid warning severity")
        expected = {
            1: "severe flood warning",
            2: "flood warning",
            3: "flood alert",
            4: "warning no longer in force",
        }
        if text(properties.get("SEVERITY"), "severity label", 100).casefold() != expected[level]:
            raise DataError("Warning severity label and code disagree")
        dates = {}
        for source, destination in (
            ("TIMERAISED", "raised"),
            ("SEVERITY_CHANGED", "severity_changed"),
            ("RIM_CHANGED", "message_changed"),
        ):
            date = timestamp(properties.get(source))
            if date > now + timedelta(minutes=5):
                raise DataError("Warning timestamp is in the future")
            dates[destination] = date.isoformat()
        # Live NRW records can have changes older than TIMERAISED. These are
        # independent source timestamps, never an inferred validity interval.
        tidal = properties.get("TIDAL")
        if isinstance(tidal, str) and tidal.lower() in ("true", "false"):
            tidal = tidal.lower() == "true"
        if tidal is not None and type(tidal) is not bool:
            raise DataError("Invalid tidal indicator")
        row = {
            "id": code,
            "area_code": code,
            "area_name": text(properties.get("DESCRIPTION"), "area name", 1000),
            "region": text(properties.get("AREA"), "region", 150),
            "severity_code": int(level),
            "severity": SEVERITIES[level],
            "tidal": tidal,
            **dates,
            "message_english": text(
                properties.get("RIM_ENGLISH"), "English message", 16000, empty=True
            ),
            "message_welsh": text(properties.get("RIM_WELSH"), "Welsh message", 16000, empty=True),
        }
        previous = latest.get(code)
        if previous is not None:
            old_time = max(previous["message_changed"], previous["severity_changed"])
            new_time = max(row["message_changed"], row["severity_changed"])
            if new_time == old_time and previous != row:
                raise DataError("Conflicting duplicate warning versions")
            if new_time <= old_time:
                continue
        latest[code] = row
    return sorted(latest.values(), key=lambda row: (row["severity_code"], row["area_code"]))


def select(rows, configured_scope):
    codes = configured_scope["area_codes"]
    chosen = [row for row in rows if not codes or row["area_code"] in codes]
    active = [row for row in chosen if row["severity_code"] < 4]
    cleared = [row for row in chosen if row["severity_code"] == 4]
    return {
        "warnings": active,
        "cleared_warnings": cleared,
        "level": SEVERITIES[min(row["severity_code"] for row in active)] if active else "none",
    }
