# LEARNINGS

## Rows

| learning id | incident | root cause | one bounded rule | canonical home | enforcement surface | independent verifier | status | evidence path |
|---|---|---|---|---|---|---|---|---|
| lrn-001 | a gate was skipped on a red run | the run had no stop condition on gate failure | a gate failure blocks advancement and is never skipped | `skills/protean-control-plane/SKILL.md` | `scripts/protean-ops/check-learnings.py` | qa | enforced | `records/LEARNINGS.md` |
