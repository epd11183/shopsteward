"""Shop operations (M8a). Lives inside pipeline/ so import-linter contracts
are unchanged (M5b/pod precedent) -- see design
docs/designs/2026-08-03-m8-autonomous-operations-draft.md.

Slice 1 is READ-ONLY: proj_listing_daily + analytics + the text brief. No
capabilities, no registry, no runner, no writes beyond opsconfig.seeded/
.updated. Those are M8a slices 2+.
"""
