# CONTRIB-STATE - invalid fixture: an expired grant and an over-cap action count

Gate: `scripts/protean-ops/check-contrib-state.py` (G-14). Expected: exit 1.

## Header (fixed keys)

| key | value |
|---|---|
| schema_version | 1 |
| internal_contrib | on |
| external_contrib | on |
| read_at | 2026-09-17T15:30:00Z |
| mode_epoch | - |
| last_change | 2026-09-01T09:00:00Z |

## Rows (append-only; fixed columns)

| row id | kind | repo | action | thread | mode | authority | detail | lane / session | time | evidence |
|---|---|---|---|---|---|---|---|---|---|---|
| cc-20260901-085900 | switch | - | - | - | external | approval op-4 | external_contrib=on operator reference op-4 | installer | 2026-09-01T08:59:00Z | records/examples/valid/decision-evidence.md |
| cc-20260901-090000 | grant | upstream/example | pr | - | external | grant g-2 | grant_id=g-2 class=pr-open scope=upstream/example expires=2026-09-08T00:00:00Z max_actions=9 | - | 2026-09-01T09:00:00Z | records/examples/valid/decision-evidence.md |
| cc-20260902-090000 | consumed | upstream/example | pr | 701 | external | grant g-2 | grant_id=g-2 consumed 1 of 9 | lane/contrib-ext | 2026-09-02T09:00:00Z | records/examples/valid/decision-evidence.md |
