# Source contract fixtures

These files are deterministic snapshots of real published items from the four
registered news sources: Dazhong, Reddit, Guardian, and Pikabu. They retain the
real canonical item identity and representative source metadata while remaining
fully offline during tests. Fixture media URLs are never downloaded by these
contract tests.

Values that change over time on community sites, such as scores and comment
counts, reflect the recorded snapshot rather than a live assertion. Rights are
conservatively marked as requiring review unless the source supplied an explicit
republication grant. A network-facing adapter must consult the source's current
official contract (or an explicitly authorized observation) and map it into the
matching typed raw model before invoking a normalizer.

No fixture test performs network access or downloads referenced media.
