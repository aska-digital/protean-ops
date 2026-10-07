# Delegation receipt (example)

A delivery receipt names the specialist lanes that produced the work, so a
delivery with no lane list is verifiably self-made. Checked by
`scripts/protean-ops/check-delegation.py` against the role manifest.

<!-- DELEGATION-RECEIPT begin -->
specialist=build lane="harbor-build" stage="s4 build" evidence="cache/harbor-build-receipt.md"
specialist=qa lane="harbor-qa" stage="s5 verify" evidence="cache/harbor-qa-verdict.md"
<!-- DELEGATION-RECEIPT end -->
