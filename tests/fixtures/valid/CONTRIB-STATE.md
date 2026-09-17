# CONTRIB-STATE - continual contribution mode (valid fixture)

Append-only. Gate: `scripts/protean-ops/check-contrib-state.py` (G-14).

## Header (fixed keys)

| key | value |
|---|---|
| schema_version | 1 |
| internal_contrib | on |
| external_contrib | off |
| read_at | 2026-09-17T15:30:00Z |
| mode_epoch | 4f2a91c8d0e7b3a5 |
| last_change | 2026-09-17T15:20:00Z switch to off, reason: no operator grant outstanding |

## Rows (append-only; fixed columns)

| row id | kind | repo | action | thread | mode | authority | detail | lane / session | time | evidence |
|---|---|---|---|---|---|---|---|---|---|---|
| cc-20260917-152000 | switch | - | - | - | external | approval op-9 | external_contrib=off operator reference op-9 | - | 2026-09-17T15:20:00Z | records/examples/valid/decision-evidence.md |
| cc-20260917-152101 | epoch | - | - | - | internal | internal-default | mode_epoch=4f2a91c8d0e7b3a5 | lane/contrib-scan | 2026-09-17T15:21:01Z | records/examples/valid/decision-evidence.md |
| cc-20260917-152210 | lane | example/project | pr | - | internal | internal-default | lease_until=2026-09-17T17:22:10Z | lane/contrib-internal | 2026-09-17T15:22:10Z | records/examples/valid/decision-evidence.md |
| cc-20260917-152240 | submitted | example/project | pr | 418 | internal | internal-default | branch=contrib/internal-gate-row head=0f1e2d | lane/contrib-internal | 2026-09-17T15:22:40Z | records/examples/valid/decision-evidence.md |
| cc-20260917-153000 | grant | upstream/example | comment | 90210 | external | grant g-7 | grant_id=g-7 class=comment scope=upstream/example thread=90210 expires=2026-09-24T00:00:00Z max_actions=1 gates=qa-pass | - | 2026-09-17T15:30:00Z | records/examples/valid/decision-evidence.md |
