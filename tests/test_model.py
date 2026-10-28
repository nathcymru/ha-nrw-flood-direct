from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import pytest
from fixtures import NOW, feature, feed

from custom_components.nrw_flood_direct.model import (
    DataError,
    normalize,
    scope,
    select,
    source_time,
    timestamp,
)


def test_all_levels_are_distinct_and_cleared_is_never_active():
    rows = normalize(feed(*(feature(f"TEST{x}", x) for x in (1, 2, 3, 4))), NOW)
    result = select(rows, scope({"mode": "all_wales"}))
    assert result["level"] == "severe_flood_warning"
    assert [x["severity_code"] for x in result["warnings"]] == [1, 2, 3]
    assert [x["severity_code"] for x in result["cleared_warnings"]] == [4]
    assert rows[0]["tidal"] is None
    assert rows[0]["severity_changed"] < rows[0]["raised"]


def test_empty_and_explicit_area_filter_are_different_from_malformed():
    selected = scope({"mode": "area_codes", "area_codes": ["TEST002"]})
    assert select(normalize(feed(feature()), NOW), selected)["level"] == "none"
    assert select(normalize(feed(), NOW), selected)["warnings"] == []
    for value in (
        None,
        {},
        [],
        {"type": "FeatureCollection"},
        {"type": "FeatureCollection", "features": None},
    ):
        with pytest.raises(DataError):
            normalize(value, NOW)


@pytest.mark.parametrize(
    "config",
    [
        {"mode": "area_codes"},
        {"mode": "area_codes", "area_codes": ["../x"]},
        {"mode": "area_codes", "area_codes": ["X", "X"]},
        {"mode": "all_wales", "area_codes": ["X"]},
        {"mode": "radius"},
    ],
)
def test_ambiguous_selection_rejected(config):
    with pytest.raises(DataError):
        scope(config)


def test_timestamp_milliseconds_and_timezone_normalization():
    assert timestamp(int(NOW.timestamp() * 1000)) == NOW
    assert timestamp("2026-10-28T10:00:00+01:00") == NOW
    assert timestamp("2026-10-28T09:00:00Z") == NOW
    for value in (NOW.timestamp(), True, float("nan"), "2026-10-28T09:00:00", "not a date"):
        with pytest.raises(DataError):
            timestamp(value)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("SEVERITYVALUE", 0),
        ("SEVERITYVALUE", "3"),
        ("SEVERITYVALUE", True),
        ("SEVERITY", "Warning no longer in force"),
        ("TIMERAISED", 0),
        ("TIMERAISED", "2026-10-28T09:00:00"),
        ("TIDAL", 1),
        ("RIM_ENGLISH", None),
        ("FWACODE", ""),
    ],
)
def test_one_bad_record_never_silently_disappears(field, value):
    bad = feature("BAD")
    bad["properties"][field] = value
    with pytest.raises(DataError):
        normalize(feed(feature("GOOD"), bad), NOW)


def test_future_record_and_unverified_crs_rejected():
    bad = feature()
    bad["properties"]["TIMERAISED"] = int((NOW + timedelta(hours=1)).timestamp() * 1000)
    with pytest.raises(DataError):
        normalize(feed(bad), NOW)
    data = feed()
    data["crs"]["properties"]["name"] = "EPSG:27700"
    with pytest.raises(DataError):
        normalize(data, NOW)


@pytest.mark.parametrize(
    "extra",
    [
        {"next": "https://example.invalid"},
        {"numberMatched": 10},
        {"links": [{"rel": "next"}]},
        {"error": "failed"},
    ],
)
def test_incomplete_or_error_response_rejected(extra):
    with pytest.raises(DataError):
        normalize({**feed(), **extra}, NOW)


def test_duplicate_resolution_and_conflict():
    first, second = feature(), feature(level=4)
    second["properties"]["RIM_CHANGED"] += 1000
    assert normalize(feed(first, second), NOW)[0]["severity_code"] == 4
    second["properties"]["RIM_CHANGED"] -= 1000
    with pytest.raises(DataError):
        normalize(feed(first, second), NOW)


def test_warning_age_does_not_imply_expiry():
    old = feature()
    for key in ("TIMERAISED", "RIM_CHANGED", "SEVERITY_CHANGED"):
        old["properties"][key] = int(datetime(2025, 1, 1, tzinfo=UTC).timestamp() * 1000)
    assert normalize(feed(old), NOW)[0]["severity_code"] == 3


def test_http_freshness_does_not_let_age_or_date_rejuvenate_each_other():
    assert timestamp(source_time({"Date": format_datetime(NOW)}, NOW)) == NOW
    for headers in (
        {},
        {"Date": "bad"},
        {"Date": format_datetime(NOW), "Age": "1800"},
        {"Date": format_datetime(NOW - timedelta(hours=1)), "Age": "0"},
        {"Date": format_datetime(NOW + timedelta(minutes=6))},
    ):
        with pytest.raises(DataError):
            source_time(headers, NOW)
