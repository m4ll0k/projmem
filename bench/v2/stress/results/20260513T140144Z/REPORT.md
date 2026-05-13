# Daemon stress test

_Run at 2026-05-13T14:01:44Z._

- leases attempted: **100**
- files: **20**
- elapsed: **15.65s** (6.4 leases/s)
- CLI opens OK: **100 / 100**
- CLI closes OK: **100 / 100**
- WS events received: **200**
- WS `leased` total: **100**
- WS `released` total: **100**
- **VERDICT: PASS**

## Missing events (should be empty on PASS)
```
missing_leased   = {}
missing_released = {}
```

## What this verifies
- The daemon's `poll_file_events` task tails the SQLite `file_event` table and broadcasts every new row.
- Every CLI-triggered lease produces a `leased` event + a `released`/`abandoned` event on the WS stream.
- The UI graph's pulsing-halo + dim-others focus mode reacts in real time to those events.

## Reproduce
```bash
python3 -m bench.v2.stress.run --leases 100 --files 20
```