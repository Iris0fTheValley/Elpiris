# Source contract fixtures

These files are deterministic inputs for the internal `RawSourceItem` adapter
contracts. Contract-only values exercise field shapes and never enter the
application story database. Any fixture used as news content in workflow or
media E2E tests must instead be a snapshot of a real published story, retain its
canonical source URL, and document its rights status. A network-facing adapter
must consult the source's current official contract (or an explicitly authorized
observation) and map it into the matching typed raw model before invoking a
normalizer.

No fixture test performs network access or downloads referenced media.
