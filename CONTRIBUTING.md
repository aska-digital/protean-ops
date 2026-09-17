# Contributing

## Running this repository

```bash
bash install.sh --target <dir> --dry-run   # print the plan, write nothing
bash install.sh --target <dir>             # install the payload into <dir>
python3 -m unittest discover -s tests      # run the test suite
```

Every declared gate is listed in `protean-ingredient.json` and runs from the
repository root, for example:

```bash
python3 gates/protean-ops/check-internal-names.py .
```

A gate exits 0 on a clean tree and non-zero on a violation. A gate failure blocks
advancement: it is never skipped.

## Change rules

- The first line of `README.md` must stay byte-for-byte equal to the `contract`
  field of `protean-ingredient.json`. A change to either changes both, and it is a
  contract change, not a documentation edit.
- The payload, the install targets, the gate names, and the `requires` set are the
  ingredient's public contract. Changing any of them is a breaking change.
- Adding a gate, an optional file, or a `recommends` edge is a compatible addition.
- Fixes that change none of the above are patch releases.
- The leak gate exists so a private identifier never reaches this repository. It
  reads digests, never names. Do not add a name to it: rebuild the digest list
  privately if the inventory changes.
- Never publish a private identifier, a machine-specific absolute path, a session
  identifier, or a credential name that enumerates a private inventory. The gate
  is the enforcement surface, and it is not a substitute for reading your diff.

## Commits and releases

- Releases are annotated `vMAJOR.MINOR.PATCH` tags on the default branch. A tag is
  never moved and never re-pointed.
- Keep one reviewable change per pull request and state the evidence in the pull
  request body.
- Attribution convention: commits carry the role codename identity, never a
  personal identity. The rule and its rationale are stated in the operating
  doctrine ingredient of the kit.

## License

MIT. By contributing you agree your contribution is licensed under the same terms.
