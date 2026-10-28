# Validation

Version 0.1.1: 67 tests passed locally on Python 3.14.10 with Home Assistant 2026.10.4. Ruff lint and formatting checks also passed.

The synthetic tests cover warning levels 1–4, true empty responses, malformed/incomplete feeds, timezone and Unix-millisecond conversion, old but active warnings, source age, durable quotas, authentication failure, redirects, oversized payloads, corrupt and symlinked ledgers, restart cache behavior, real YAML secrets/config-entry construction, polygon holes/boundaries, complete WFS results, identifier joins, location changes, successful empty area selections and persistent provider retry delays.

Version 0.1.1 regressions exercise active nationwide warnings with a successful empty home selection: the main sensor remains available with `none`, all counts are zero, and no national records leak into the home result. This survives a cached restart. Failed or stale feeds and failed, stale or missing area selection still publish `unavailable` through actual Home Assistant entity classes.

Read-only live checks on 28 October 2026 established the actual NRW v3 field types and complete WFS 2.0 intersection response shape. Public point probes verified both polygon layers and local membership; one authenticated feed request verified nullable tidal values, millisecond timestamps and separate cleared records. Private home-area selection is deployment-specific and is not included in this repository.

No live warning can be manufactured for acceptance. Renderer warning states are tested with explicit synthetic fixtures; deployment verification checks the provider's actual current state and coverage independently.
