The dispatch/rotation/inflight ops kit: append-only record schemas and the deterministic gates that check them.

# protean-ops

The records and gates ingredient of the Protean Kit distribution. It carries the
five append-only record schemas, the roster allowlist, and the five read-only
gates that check them.

## What it installs and where

| Path | Contents |
|---|---|
| `records/` | schema templates `.md.tmpl` (fixed columns, empty state), `ROSTER.txt` |
| `scripts/protean-ops/` | the five gate scripts |
| `gates/protean-ops/` | the leak gate and the blocklist |

## Install

```bash
bash install.sh --target <dir>
bash install.sh --target <dir> --dry-run
```

Bash and coreutils only, zero network calls, every written path printed, no
`--target` means no run. A dry run writes nothing.

Installs alone with this command, resolving only its required dependencies listed
in its manifest entry. Optional relationships are reported, not fetched.

## Requirements and recommendations

Requires none. Recommends none.

## Use

Copy the schema templates to the record names the gates expect, then let a role
append rows. The gates read a record; they never write one:

```bash
python3 scripts/protean-ops/check-rotation.py <records>/ROTATION-STATE.md
python3 scripts/protean-ops/check-inflight.py <records>/INFLIGHT.md
python3 scripts/protean-ops/check-learnings.py <records>/LEARNINGS.md
python3 scripts/protean-ops/check-hotpath-freeze.py <manifest.md5>
python3 scripts/protean-ops/check-decision-report.py <report.html> [--manifest M] [--evidence E]
```

Record resolution defaults to `./records` and can be redirected with
`$PROTEAN_RECORDS_ROOT`. The rotation roster comes from `--roster`,
`$PROTEAN_ROSTER`, or `ROSTER.txt` beside the record, and the gate fails closed
when none is available. Every gate is read-only, stdlib-only, `--help`-able, and
exits 0 pass, 1 violation, 2 missing or unparseable input.

The record templates ship as schema with an empty state and carry no live row.
The shipped examples under `records/examples/` are synthetic fixtures the gates
run against, not record content.

## Gates

| Gate | Command (declared) |
|---|---|
| internal-name gate | `python3 gates/protean-ops/check-internal-names.py .` |
| rotation | `python3 scripts/protean-ops/check-rotation.py tests/fixtures/valid/ROTATION-STATE.md` |
| inflight | `python3 scripts/protean-ops/check-inflight.py records/examples/valid/INFLIGHT.md` |
| learnings | `python3 scripts/protean-ops/check-learnings.py records/examples/valid/LEARNINGS.md` |
| hot-path freeze | `python3 scripts/protean-ops/check-hotpath-freeze.py records/examples/valid/hotpath-manifest.md5` |
| decision report | `python3 scripts/protean-ops/check-decision-report.py records/examples/valid/decision-report.html --evidence records/examples/valid/decision-evidence.md` |

The declared commands run against the shipped fixture records under
`records/examples/`, so a fresh clone is verifiable and the same commands work
after installation. Point them at your own records root once you have live
records.
`tests/test_gates.py` gives every gate a green fixture and at least one
single-cause red fixture. `tests/test_install.py` proves the installer and its
dry run.

## Offline and cache behaviour

Used through the composer, this ingredient is fetched once from its pinned tag
and reused from a content-addressed cache keyed by commit SHA. `--offline`
performs zero network calls and fails closed when the cache entry is absent.

## Limits and open items

The schemas are shipped empty by design. The kit carries no adapter for a task
tracker or an external ledger: a record is a file a role appends to. Retention
windows and the home of the authorization records are unresolved items of the
protocol ingredient, not of this one.

## License

MIT. The committed `LICENSE` file is authoritative.
