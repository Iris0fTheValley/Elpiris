# Source evidence and media verification

This project accepts only Guardian, Reddit, Pikabu, and Dazhong content. Source
evidence and downloadable media are separate artifacts with separate trust
boundaries.

## Evidence cards

Raw full-page screenshots are not production inputs because they commonly
include navigation, ads, consent dialogs, recommendations, and login panels.
`frontend/scripts/quality/capture-source-evidence.mjs` instead extracts article
semantics and renders a deterministic evidence card.

The capture command validates the registered source host, rejects ad-like and
banner-shaped image candidates, and writes a provenance sidecar next to the PNG.
The sidecar is mandatory and keeps `publish_eligible=false` until an operator
records sufficient reuse rights.

```powershell
Set-Location frontend
pnpm capture:source-evidence -- --source guardian --url <canonical-url> --output <png-path>
```

When a source blocks or rate-limits browser access, use an already normalized
real-source fixture with `--fixture`. The sidecar then records
`normalized_source_fixture`; it must never be presented as a live browser
capture.

## Real source video

Authorized Pikabu and Dazhong public pages now carry direct MP4 references
from article video/source tags, article links, Open Graph/Twitter video metadata,
and JSON-LD `VideoObject.contentUrl` into normalized media provenance. The
Reddit OAuth adapter pages through the authorized subreddit `/new` listing
using its `after` cursor, preserving listing order and dropping repeated post IDs.
It stops at the configured item budget, an exhausted or repeated cursor, an empty
page, the reported API rate limit, or ten pages. A later API error retains the
posts already collected and marks the run partial. Post self-text remains the
normalized story text when present; a link post retains its HTTP(S) destination
in source provenance without fetching that page. The adapter checks secure,
ordinary, and preview video payloads in order and records the first direct MP4;
it also accepts a direct MP4 outbound URL when those payloads contain no usable
video. The Jina Reader
layer requests rendered HTML in its JSON response, so the same page snapshot
supplies article text and direct MP4 references from video/source tags, article
links, Open Graph/Twitter metadata, and JSON-LD. If Reader does not supply the
requested HTML, the fetch chain tries its next layer instead of treating a
text-only result as complete media evidence. Duplicate links are discarded and
discovery is capped at 50 links per page.

The URL fetch layers extract article text with Trafilatura where available and
fall back to visible article/main text for sparse or rendered pages. The text
is normalized consistently before it enters a story. A titled video page can
use its title as minimal text when no article body exists. Embedded players,
streaming manifests, and other formats are not treated as downloadable source
videos: the current acquisition boundary verifies MP4 bytes and ffprobe
metadata. Discovery records a candidate; an editor still acquires it manually,
and the existing rights review remains mandatory. Acquisition retries transient
HTTP 429/5xx responses and interrupted downloads, but redirects still require
the destination URL to be captured and reviewed first.

Run the production acquisition boundary against the checked-in real Pikabu
fixture:

```powershell
uv run python scripts/quality/verify_real_source_media.py
```

The verifier downloads through `HttpSourceMediaDownloader`, stores bytes
atomically, probes them with ffprobe, verifies hashing and idempotent reacquire,
and emits a rights-aware JSON report. Interrupted downloads use validated HTTP
Range resumption. A server that ignores or contradicts the requested range is
rejected instead of risking a corrupted file.

Acquisition streams into a private `.partial` file and probes those bytes before
publishing them. Network transfer and ffprobe do not hold the shared asset
lifecycle lock, so visual uploads, media lifecycle changes, and retention can
proceed during a slow source download. The final rename and repository claim
remain under that lock; retention ignores the staged file and sees only a
claimed MP4. A story version change before publication rejects the staged
download and removes its bytes.

The fixture remains `permission_required` and therefore
`publish_eligible=false`. Successful technical verification is not publication
permission.

## End-to-end render

The E2E runner downloads the real source media by default. During repeated local
render tests, pass a previously generated verification report to reuse the same
immutable bytes without repeatedly hitting the source:

```powershell
uv run python scripts/quality/run_e2e_video.py `
  --verified-source-report <verification-report.json>
```

Before reuse, the runner rechecks canonical and source URLs, SHA-256, file size,
and ffprobe metadata. The source clip keeps its original audio and receives a
separate translated caption track.
