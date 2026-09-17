The dispatch/rotation/inflight ops kit: append-only record schemas and the deterministic gates that check them.

# protean-ops

The records and gates ingredient of the Protean Kit distribution. It carries the
five append-only record schemas, the roster allowlist, and the five read-only
gates that check them.

## Do you need this?

ROLE: The records and gates ingredient. Five append-only record schemas with a roster allowlist, checked by five deterministic read-only gates.

USE WHEN:
- A run must keep rotation state, inflight work, learnings, a hot-path freeze manifest, and decision reports as append-only records copied from the schema templates.
- A gate must verify a record with a stdlib-only script that reads only, exits 0 pass, 1 violation, or 2 missing or unparseable input, redirectable with `PROTEAN_RECORDS_ROOT`.
- A fresh clone must be verifiable. The declared gate commands run against the synthetic fixtures under `records/examples/`.

SKIP WHEN:
- The need is state mutation by the gate itself. Gates never write a record, and templates ship with empty state and no live rows.
- The need is prose, draft rendering, protocol validation, or GitHub flow. Those live in their own ingredients.

## What it installs and where

| Path | Contents |
|---|---|
| `records/` | schema templates `.md.tmpl` (fixed columns, empty state), `ROSTER.txt` |
| `scripts/protean-ops/` | the six gate scripts |
| `gates/protean-ops/` | the leak gate and the blocklist |

The six gates are rotation, inflight, learnings, hot-path freeze, decision
report, and the contribution-state gate (G-14).

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
python3 scripts/protean-ops/check-contrib-state.py <records>/CONTRIB-STATE.md
```

The contribution-state gate answers two questions from one append-only record:
whether the recorded state is legal, and whether a queried write may happen now.
Its second half is time-aware, so it takes an explicit target:

```bash
python3 scripts/protean-ops/check-contrib-state.py <records>/CONTRIB-STATE.md \
  --target external --repo <owner/name> --action pr
```

Exit 0 means the state is legal and the queried write is inside the lane cap,
the rate windows, the quiet hours, and the grant rules; exit 1 refuses the
write. Two defaults are contractual: `internal_contrib` is always on (`off` is
refused unless the record cites `decision: <id>`), and an absent or unreadable
record reads as `external_contrib: off`, so a remote write fails closed while a
local branch, commit, test run, and rendered draft stay allowed.

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
| contribution state | `python3 scripts/protean-ops/check-contrib-state.py records/examples/valid/CONTRIB-STATE.md` |

The declared commands run against the shipped fixture records under
`records/examples/`, so a fresh clone is verifiable and the same commands work
after installation. Point them at your own records root once you have live
records.
`tests/test_gates.py` gives every gate a green fixture and at least one
single-cause red fixture. `tests/test_contrib_state.py` covers the
contribution-state gate's toggle, lane, rate, quiet-hour, and grant rules.
`tests/test_install.py` proves the installer and its dry run.

## Offline and cache behaviour

Used through the composer, this ingredient is fetched once from its pinned tag
and reused from a content-addressed cache keyed by commit SHA. `--offline`
performs zero network calls and fails closed when the cache entry is absent.

## Limits and open items

The schemas are shipped empty by design. The kit carries no adapter for a task
tracker or an external ledger: a record is a file a role appends to. Retention
windows and the home of the authorization records are unresolved items of the
protocol ingredient, not of this one.

Two limits of the contribution-state gate are stated rather than hidden. Live
repository backlog counts and thread attention metrics are not derivable from a
local record, so the gate does not claim them: a lane reads them live and
records the raw output in its own evidence file. The quiet-hours rule needs a
timezone database for `America/New_York`; when none is available the gate
refuses an external write instead of passing it.

## License

MIT. The committed `LICENSE` file is authoritative.
