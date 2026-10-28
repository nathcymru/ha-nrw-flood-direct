# NRW Flood Direct

Natural Resources Wales live flood warnings and alerts for Home Assistant. One shared feed supplies every sensor; the integration keeps NRW flood information separate from weather forecasts.

## Install

Requires Home Assistant 2026.10 or later. Add this repository to HACS as an **Integration**, download it, and configure an existing NRW **Live Flood Warnings & Alerts API Data** subscription from the [official portal](https://api-portal.naturalresources.wales/).

Put your subscription key in `secrets.yaml` and keep that file private:

```yaml
nrw_flood_direct_api_key: "YOUR_EXISTING_SUBSCRIPTION_KEY"
```

In `configuration.yaml`:

```yaml
nrw_flood_direct:
  api_key: !secret nrw_flood_direct_api_key
  mode: home
```

Check configuration and restart Home Assistant. The imported integration entry contains no credentials. A rejected key stops API requests until the integration is reloaded with corrected YAML.

## Location and area selection

`home` uses the Home Assistant home coordinates and the official NRW **Flood Warning Areas** and **Flood Alert Areas** polygons published through DataMapWales. It requests an exact point intersection for each layer, verifies complete result counts and matching identifiers, then independently checks polygon membership including holes and boundaries. There is no radius or nearest-centroid fallback. The coordinates are sent only to the official DataMapWales service and are not exposed as entity attributes or stored in this integration's cache; a local location fingerprint invalidates matches when home moves.

Geometry refreshes once a day, with a persistent cache, bounded responses, a maximum of 20 lookup requests per UTC day, and provider retry delays. A normal refresh uses two lookup requests. Failed, unresolved or stale area selection makes warning/count sensors unavailable. Last-known area codes remain in the private cache for recovery, never as current data.

The live API reports current warnings; the published polygons separately define geographic areas used to select those warnings. A successful complete lookup with no matching polygons is a valid empty selection. With a fresh successful live feed, the warnings sensor reports `none` and all counts are zero: **no matching active warnings**. It does not report a coverage failure or select warnings from elsewhere in Wales.

The diagnostic `sensor.nrw_flood_direct_coverage_status` reports `no_matching_areas` for that empty selection. Its stable entity ID and the `coverage_status` attribute are retained for compatibility; the diagnostic describes polygon matching, not overall NRW service coverage. It does not establish flood risk. NRW's warning service does not cover every source of flooding, including surface water and small watercourses. See [NRW's explanation of its warning service](https://naturalresources.wales/flooding/sign-up-to-receive-flood-warnings/?lang=en).

Alternative explicit selections:

```yaml
nrw_flood_direct:
  api_key: !secret nrw_flood_direct_api_key
  mode: area_codes
  area_codes: ["YOUR_VERIFIED_FWACODE"]
  area_label: "Selected flood areas"
```

Or select `mode: all_wales` without `area_codes`. That scope is always labelled **All Wales**. Changing scope requires a configuration reload/restart. Area-code selection does not independently verify that an arbitrary code exists; use the official published identifiers.

## Entity contract

Every entity is enabled by default. The primary entity is `sensor.nrw_flood_direct_warnings`, with state `severe_flood_warning`, `flood_warning`, `flood_alert`, or `none`. Failed, malformed or stale feeds and failed, unresolved or stale area selection produce Home Assistant's `unavailable`, never `none` or a zero count. Successful empty matching results produce `none` and zero counts while the data remain fresh.

Its attributes include:

| Attribute | Meaning |
| --- | --- |
| `warnings` | Active severity 1, 2 and 3 records, or null when unavailable |
| `cleared_warnings` | Separate severity 4 records; never active warnings |
| `scope` | `{type: area_codes or all_wales, label, area_codes}`; home mode also includes `selection_method`, `coverage_status`, `resolved_at` |
| `fetched_at` | Successful feed retrieval time, ISO UTC |
| `response_time` | HTTP Date/Age freshness bound, not the warning's issue date |
| `stale_after_seconds` | 1800 |
| `coverage_note` | Human-readable area-selection diagnostic and limitations |

Each normalized record has `id`, `area_code`, `area_name`, numeric `severity_code`, `severity`, `region`, nullable `tidal`, `raised`, `severity_changed`, `message_changed`, `message_english`, and `message_welsh`. Dates are ISO UTC; NRW's numeric Unix-millisecond timestamps are normalized explicitly. A record's change dates can precede its raised date. Those dates are never treated as an expiry time. No per-area link is invented.

Additional sensors provide active total, each active severity count, separate cleared count, API status, last successful update, daily requests, and area-selection status. Feed availability, polygon matching and warning counts are separate facts.

Home Assistant removes extra attributes from unavailable entities. Consumers can read `sensor.nrw_flood_direct_coverage_status` for the current selection diagnostic when the main warnings entity is unavailable; that diagnostic remains available and includes the same `scope` object. A selection diagnostic alone never proves the live feed is fresh. Do not treat a missing main-entity attribute as an empty warning list.

## Freshness, quotas and privacy

The integration retrieves the complete nationwide GeoJSON feed once every 15 minutes, shared by all sensors: normally 96 calls/day. Its local ceiling is 2700 calls per UTC day, below the portal's published 2880/day allowance. Each request is durably reserved before transmission, with at least six seconds between requests. The ledger survives restarts; a corrupt ledger blocks requests rather than resetting limits. Account use by other applications is outside this local counter.

Provider 429 retry delays, authentication rejection, network backoff, fixed HTTPS origins, no redirects, 4 MiB body limits and bounded request timeouts prevent uncontrolled retries. Source failures immediately invalidate current warning availability; a fresh cached response survives restart without an extra request. Failed attempts cannot resurrect an older successful response. A successful empty matching set means **no matching warnings**, not no flood risk.

API keys stay in YAML secrets and runtime memory. They are not copied into config entries, logs, diagnostics, entity attributes or public assets. Keep `.storage/nrw_flood_direct_runtime.json` when restoring a deployment so request counters are not reset.

## Data sources and attribution

- [NRW Live Flood Warnings and Alerts API v3](https://api-portal.naturalresources.wales/api-details#api=open-data-live-flood-warnings-and-alerts-api-version-3)
- [NRW Flood Warning Areas](https://datamap.gov.wales/layers/inspire-nrw%3ANRW_FLOOD_WARNING)
- [NRW Flood Alert Areas](https://datamap.gov.wales/layers/inspire-nrw%3ANRW_FLOOD_WATCH_AREAS)

Contains Natural Resources Wales information © Natural Resources Wales and Database Right. All rights Reserved. Contains Ordnance Survey Data. Ordnance Survey Licence number AC0000849444. Crown Copyright and Database Right. The published area layers use the Open Government Licence.

Independent community software, not an official NRW application. Code is MIT licensed. Transport/accounting and polygon-membership patterns are adapted from [ha-metoffice-direct](https://github.com/nathcymru/ha-metoffice-direct), copyright 2026 nathcymru, under the same MIT licence.

## Development

Run `pytest`, `ruff check .`, and `ruff format --check .` with Python 3.14 and Home Assistant 2026.10.4. Tests use synthetic warnings and HTTP responses, including actual Home Assistant configuration/entity classes. They do not spend API quota.
