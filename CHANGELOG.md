# Changelog

All notable changes to this repository are recorded here. The format is a short
entry per release: what changed, why, and how it was verified.

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
