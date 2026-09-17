# Tick receipts

One receipt per launched sweep, written by the sweep itself at the path the tick handed it
(`$PROTEAN_WATCHDOG_RECEIPT`, normally `<tick-id>.md` in this directory).

A receipt is distinct from the watchdog event row in `records/idle-watchdog-ticks.jsonl`:

- the **event row** proves admission behaviour - which guard ran, what it observed, and whether any
  child was launched;
- the **receipt** proves the canonical decision and its terminal handling.

A receipt whose last non-empty line is not one of `STABLE`, `CLOSED`, `PASS`, or `COMPLETE` is not
completion evidence. A fired event row with no receipt is an operational defect: the next recovery
pass surfaces it, and it is never converted into another automatic launch.

This directory is seeded empty. Nothing here is deleted automatically - receipts are evidence and
are pruned, if ever, only by an explicit operator action.
