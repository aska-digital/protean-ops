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

The idle watchdog is not part of the installed payload: it runs from this
checkout via absolute paths. Its files are `scripts/idle-watchdog.py`,
`briefs/idle-watchdog-sweep.md`, the seed `records/IDLE-WATCHDOG-STATE.json`,
the append-only `records/idle-watchdog-ticks.jsonl`, the
`records/tick-receipts/` directory, and the cron specification
`cron/idle-watchdog.cron`.

## Idle watchdog (shipped, not installed)

`scripts/idle-watchdog.py` is a launch admission gate for a scheduled liveness
tick: it reads durable state, applies an ordered guard pipeline (singleton
lock, kill switch, authoritative process registry, project state record,
shared-record gates, cooldown and one-fire fingerprint, daily attempt cap,
quiet hours), and either launches exactly one evaluator sweep or records a
skip. It is not a contribution evaluator: it selects no target, claims no
epoch, performs no closure pass, and writes nothing outside its own state,
event log, sweep logs, and lock. Missing, malformed, stale, or uncertain state
always skips; the kill switch is never flipped by this process; a pending
reservation is never cleared by age (only the explicit recovery subcommand,
under the same lock, with an operator assertion).

Installation is an operator action this repository does not perform. Read
`cron/idle-watchdog.cron`, replace its placeholders with absolute paths, keep
every double quote, add the single `*/15 * * * *` line to the crontab, and
verify with `crontab -l | grep -c idle-watchdog.py` (must print `1`). Do not
install a second line and do not flip the contribution-state toggle as part of
installing the tick. Until then the watchdog never runs.

Safety properties are asserted by `tests/test_idle_watchdog.py` (fixture and
subprocess tests only; temporary roots, fake gates, a fake launcher; no live
records, no live profile database, no real evaluator session):

```bash
python3 -m unittest tests.test_idle_watchdog -v
```

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
cp records/ROTATION-STATE.md.tmpl records/ROTATION-STATE.md
cp records/INFLIGHT.md.tmpl records/INFLIGHT.md
cp records/LEARNINGS.md.tmpl records/LEARNINGS.md
cp records/CONTRIB-STATE.md.tmpl records/CONTRIB-STATE.md

python3 scripts/protean-ops/check-rotation.py records/ROTATION-STATE.md
python3 scripts/protean-ops/check-inflight.py records/INFLIGHT.md
python3 scripts/protean-ops/check-learnings.py records/LEARNINGS.md
python3 scripts/protean-ops/check-contrib-state.py records/CONTRIB-STATE.md
```

Every template carries its record's table header, so a copied template is read as a
record and not as a missing file. A record with no row yet is not yet checkable: the
rotation and learnings gates report missing input (exit 2), never a violation, until
the first row is appended. The in-flight and contribution-state templates ship an
explicit empty-state row, so those two pass before any live row exists. The rotation
roster comes from `ROSTER.txt` beside the record, which the payload ships at
`records/ROSTER.txt`.

The hot-path freeze gate and the decision-report gate need artifacts no template
ships, a freeze manifest and a rendered report, so they are declared against the
synthetic fixtures under `records/examples/`.

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
| rotation | `python3 scripts/protean-ops/check-rotation.py records/examples/valid/ROTATION-STATE.md --roster records/examples/valid/ROSTER.txt` |
| inflight | `python3 scripts/protean-ops/check-inflight.py records/examples/valid/INFLIGHT.md` |
| learnings | `python3 scripts/protean-ops/check-learnings.py records/examples/valid/LEARNINGS.md` |
| hot-path freeze | `python3 scripts/protean-ops/check-hotpath-freeze.py records/examples/valid/hotpath-manifest.md5` |
| decision report | `python3 scripts/protean-ops/check-decision-report.py records/examples/valid/decision-report.html --evidence records/examples/valid/decision-evidence.md` |
| contribution state | `python3 scripts/protean-ops/check-contrib-state.py records/examples/valid/CONTRIB-STATE.md` |

The declared commands run against the shipped fixture records under
`records/examples/`, so a fresh clone is verifiable and the same commands work
after installation. Point them at your own records root once you have live
records. This table is the same set of commands `protean-ingredient.json`
declares; that descriptor is authoritative if the two ever disagree, and
`tests/` is not part of the installed payload, so no declared command may point
into it.
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
