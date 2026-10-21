# Eligibility rules

Three different eligibility questions exist; do not mix them.

## 1. Live snapshot observation (collector)

A station row is `eligible = 1` only when all hold:

- `is_installed`, `is_renting`, and `is_returning` are all true.
- `num_bikes_available` and `num_docks_available` are non-negative integers.
- `last_reported` parses and is not older than `max(300s, 3 x feed TTL)`.
- The feed `last_updated` is not older than the same threshold.
- The station has a matching row in `station_information`.

Closed, stale, malformed, and unmatched rows stay in the raw archive and in the
`observations` table for analysis, but never drive decisions. Repeated source
payloads are flagged `repeated_source` and are not treated as new states.

## 2. Historical station-time targets (Session 2)

The public trip archive has no station opening/closing dates, so v0 uses a
documented heuristic:

- A station is assumed operating from its first to its last observed trip day
  (inclusive), per station, across the loaded months.
- Windows inside that span are emitted with `eligible = 1`. Absent trips there
  are genuine zeros and are zero-filled.
- Windows outside the span are not emitted at all: unevaluable, never zeros.
- Trip IDs without a verified live match (`mapping_status != matched`) are
  excluded entirely; they are not guessed or merged.
- Timestamps are NYC local converted to UTC; DST-ambiguous or nonexistent
  wall times are excluded rather than assigned.

Limitations: real operating history (temporary closures, seasonal stations,
capacity changes) is unknown; a station closed mid-month may receive zero-filled
windows it did not operate. This is acceptable for v0 and must be revisited when
archived metadata history exists.

## 3. Actionable queue (API)

`/queue` only considers stations that pass all of:

- Live snapshot flags all true (same as rule 1).
- `num_bikes_available + num_docks_available > 0` — a stall with zero usable
  docks and zero bikes (typically disabled assets) cannot receive or give bikes.
- The latest snapshot is served with its age; requests older than 10 minutes
  are marked `snapshot_stale = true` rather than silently used.

Recommendations respect physical possibility: `add_bikes` only when usable
docks exist, `remove_bikes` only when bikes exist, otherwise `monitor`.
The `warning_score` is a transparent priority score, not a calibrated
probability.
