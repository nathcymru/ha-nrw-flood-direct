from copy import deepcopy
from datetime import UTC, datetime

NOW = datetime(2026, 10, 28, 9, tzinfo=UTC)
LABELS = {
    1: "Severe Flood Warning",
    2: "Flood Warning",
    3: "Flood Alert",
    4: "Warning no longer in force",
}


def feature(code="TEST001", level=3):
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [-3, 53]},
        "properties": {
            "FWACODE": code,
            "AREA": "North",
            "DESCRIPTION": "Synthetic flood area",
            "TIDAL": None,
            "SEVERITY": LABELS[level],
            "SEVERITYVALUE": level,
            "TIMERAISED": int(NOW.timestamp() * 1000 - 60000),
            "SEVERITY_CHANGED": int(NOW.timestamp() * 1000 - 120000),
            "RIM_CHANGED": int(NOW.timestamp() * 1000 - 60000),
            "RIM_ENGLISH": "Synthetic warning only.",
            "RIM_WELSH": "Rhybudd prawf.",
        },
    }


def feed(*features):
    return {
        "type": "FeatureCollection",
        "crs": {"type": "name", "properties": {"name": "EPSG:4326"}},
        "features": deepcopy(list(features)),
    }
