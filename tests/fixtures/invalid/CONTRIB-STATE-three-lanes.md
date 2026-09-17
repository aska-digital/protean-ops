# CONTRIB-STATE - invalid fixture: three live contribution lanes

Gate: `scripts/protean-ops/check-contrib-state.py` (G-14). Expected: exit 1
(cap is 2 lanes, of which at most 1 external).

## Header (fixed keys)

| key | value |
|---|---|
| schema_version | 1 |
| internal_contrib | on |
| external_contrib | on |
| read_at | 2026-09-17T15:30:00Z |
| mode_epoch | 4f2a91c8d0e7b3a5 |
| last_change | 2026-09-17T15:20:00Z |

## Rows (append-only; fixed columns)

| row id | kind | repo | action | thread | mode | authority | detail | lane / session | time | evidence |
|---|---|---|---|---|---|---|---|---|---|---|
| cc-20260917-152000 | switch | - | - | - | external | approval op-9 | external_contrib=on operator reference op-9 | installer | 2026-09-17T15:20:00Z | records/examples/valid/decision-evidence.md |
| cc-20260917-152201 | lane | example/project | pr | - | external | approval op-9 | lease_until=2026-09-17T17:22:01Z | lane/contrib-ext-1 | 2026-09-17T15:22:01Z | records/examples/valid/decision-evidence.md |
| cc-20260917-152202 | lane | example/project-two | pr | - | external | approval op-9 | lease_until=2026-09-17T17:22:02Z | lane/contrib-ext-2 | 2026-09-17T15:22:02Z | records/examples/valid/decision-evidence.md |
| cc-20260917-152203 | lane | example/project | issue | - | internal | internal-default | lease_until=2026-09-17T17:22:03Z | lane/contrib-int | 2026-09-17T15:22:03Z | records/examples/valid/decision-evidence.md |
