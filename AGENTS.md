# BikeFlow AI — agent rules

## Active logging (mandatory)

Every long-running command or pipeline MUST print timestamped progress lines
while it works, so a watching user can tell it is alive, what stage it is in,
and whether it died. Silence longer than ~2 minutes is a bug.

- Each line: wall-clock UTC + elapsed seconds since start, e.g.
  `[09:21:05 UTC +  12.3s] Stage scan: chunk 20 (2,000,000 rows so far)`.
- Log every stage boundary (start + done with counts), chunked loops (every N
  chunks), and per-station/per-item loops (every 500 items with a running
  total). Never log every single item.
- Flush every line. The existing `log()` helper in `src/bikeflow/session2.py`
  is the pattern to copy.
- The live collector already satisfies this by printing one JSON summary per
  poll; keep that behavior.
