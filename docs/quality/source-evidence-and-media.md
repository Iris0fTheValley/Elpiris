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
