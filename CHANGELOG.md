# Changelog

All notable changes to this repository are recorded here. The format is a short
entry per release: what changed, why, and how it was verified.

## Unreleased

- **What:** add the idle-watchdog launch admission gate: `scripts/idle-watchdog.py`
  (cron entry point with the locked guard order - singleton flock, kill-switch
  read first after the lock, authoritative process-registry adapter, project
  state record parse without any closure pass, shared-record gate health,
  two-hour cooldown from the last attempt, minute-free one-fire base
  fingerprint, four attempts per UTC day, 05:00-21:00 `America/New_York`
  window, final toggle recheck, transactional pre-spawn reservation, exactly
  one profile-native `hermes chat --oneshot --query-file` child), the sweep
  brief `briefs/idle-watchdog-sweep.md`, the watchdog schema and seed
  `records/IDLE-WATCHDOG-STATE.json`, the append-only redacted event log
  `records/idle-watchdog-ticks.jsonl`, `records/tick-receipts/` with its
  readme, the focused suite `tests/test_idle_watchdog.py`, and the shipped-not-installed
  cron specification `cron/idle-watchdog.cron`.
- **Boundary:** the gate observes and admits; it never evaluates, claims an
  epoch, flips the kill switch, deletes a lock by age, or auto-clears a pending
  reservation. Nothing installs the crontab line: that is an operator action
  after build, QA, and integration approval, and it does not include flipping
  the contribution-state toggle.
- **Verified:** `tests/test_idle_watchdog.py` runs 78 fixture and subprocess
  cases against temporary roots, fake gate executables, a fake process probe,
  and a fake launcher; every case asserts the event row and the child
  invocation count. No case touches a live record, a live profile database,
  or a real evaluator session.

## 1.1.0

- **What:** add the contribution-state record and its gate. `records/CONTRIB-STATE.md.tmpl`
  carries the fixed header and the eleven fixed columns of the continual-contribution state:
  the two toggle values, the append-only row kinds, and the counters and grants the gate
  recomputes from timestamps. `scripts/protean-ops/check-contrib-state.py` (gate G-14) is a
  read-only, stdlib-only, `--help`-able checker that answers whether the recorded state is
  legal and whether a queried write may happen now.
- **Contract of the two modes:** the internal half is always on and is asserted by the gate,
  which refuses `internal_contrib: off` unless the record cites `decision: <id>` for a
  recorded operator decision. The external half is one flag, default `off`, and fails closed:
  an absent or unreadable record, or a value outside `{on, off}`, reads as `off`, so no remote
  write of any kind happens, including a push to a fork, while a local branch, commit, test
  run, and rendered draft stay allowed. The gate also enforces the lane cap, the per-window
  rate table, the per-thread write limit, the quiet-hours window, the per-repository burst
  detector, and the grant rules, and it refuses `external_contrib: on` unless a `switch` row in
  the same record cites the operator reference that ordered it.
- **Why:** the contribution rule (which lives in the control-plane skill) is procedure law and
  the toggle value is state, so the value needs one append-only home with a deterministic
  checker and a failure mode that is safe when the record is missing. Without a record, "is
  external contribution allowed?" has no answer, and a default of "allowed" is the unsafe one.
- **Evidence class:** [VERIFIED - internal operating record] for the rule and the bounded
  values; the checker and the record schema are exercised here by `tests/test_contrib_state.py`,
  by the shipped fixtures under `records/examples/`, and by `check-contrib-state.py --self-test`.
- **Verification:** the declared gate `op-contrib-state` runs green against
  `records/examples/valid/CONTRIB-STATE.md`; `python3 -m unittest discover -s tests` passes;
  `check-contrib-state.py --self-test` reports 19 of 19 focused cases.
- **Limits:** live repository backlog counts and thread attention metrics are not derivable
  from a local record and are deliberately not claimed by this gate; a lane reads them live.
  The quiet-hours check needs a timezone database for `America/New_York` and refuses an external
  write when none is available.
- **License:** MIT. The committed `LICENSE` file is authoritative.

## 1.0.0

- **What:** the first release of the `protean-ops` ingredient: the capability payload,
  the machine descriptor `protean-ingredient.json`, the standalone installer
  `install.sh`, the declared gates under `gates/protean-ops/`, and the test suite under
  `tests/`.
- **Why:** the capability was previously reachable only from inside a private
  working tree. This repository is its single public home, installable on its own.
- **Verification:** the declared gates and `python3 -m unittest discover -s tests`
  pass from a clean checkout of this commit. See the pull request body for the
  commands and their output.
- **Contract:** `The dispatch/rotation/inflight ops kit: append-only record schemas and the deterministic gates that check them.`
- **License:** MIT. The committed `LICENSE` file is authoritative.
