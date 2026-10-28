"""One authoritative NRW feed, independent of weather providers."""

DOMAIN = "nrw_flood_direct"
NAME = "NRW Flood Direct"
ENDPOINT = "https://api.naturalresources.wales/floodwarnings/v3/all"
POLL_SECONDS = 900
STALE_SECONDS = 1800
MAX_BYTES = 4 * 1024 * 1024
MAX_FEATURES = 2000
DAILY_LIMIT = 2700
MIN_REQUEST_SECONDS = 6
SEVERITIES = {
    1: "severe_flood_warning",
    2: "flood_warning",
    3: "flood_alert",
    4: "no_longer_in_force",
}
ATTRIBUTION = "Flood warning data supplied by Natural Resources Wales"
