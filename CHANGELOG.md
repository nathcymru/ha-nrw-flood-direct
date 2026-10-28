# Changelog

## 0.1.1 — 28 October 2026

- Treat a successful complete empty home-area lookup as a valid selection. A fresh successful warning feed now produces `none` and zero counts when no warnings match, instead of unavailable sensors.
- Report the separate polygon-selection diagnostic as `no_matching_areas`, replacing `outside_known_areas`. Remove wording that equated an empty selection with missing NRW service coverage.
- Select nationwide warnings only for explicit `all_wales` scope. An empty home selection never becomes a national wildcard.
- Keep failed, unresolved and stale data unavailable. Add regressions for empty selection, nationwide active records, cached restart and invalid-data transitions.

## 0.1.0 — 28 October 2026

- Initial HACS release with the NRW v3 live feed, official home-area polygon matching, YAML secrets, request budgets and warning/count diagnostics.
