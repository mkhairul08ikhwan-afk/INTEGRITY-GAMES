# Debug Session: live-qr-rank

Status: [OPEN]

## Symptoms
- Logo shown in the app is not the user-provided original PNG.
- QR codes still fail in some rendered states, especially print/runtime fallback paths.
- Game selection behavior does not consistently enforce exactly 8 games in the active deployed flow.
- Leaderboard/ranking appears wrong or stale in live usage.

## Falsifiable Hypotheses
1. The workspace still does not contain the real PNG logo, so the frontend can only show the fallback SVG asset.
2. Some QR views still use an old DOM path or stale deployed asset bundle instead of the backend PNG-first renderer.
3. The frontend and backend both contain exact-8 logic, but one active UI path still bypasses the disable/validation flow.
4. Leaderboard ranking logic on the backend is correct, but the frontend state refresh/render path is stale or not triggered after scans.
5. A stale Render/browser cache is still serving older frontend assets that do not match the current repository code.

## Plan
1. Confirm the current local code paths for logo, QR, exact-8 selection, and leaderboard rendering.
2. Identify any remaining stale asset references or alternate UI flows.
3. Apply the smallest code changes needed to make all four paths consistent.
4. Run diagnostics and targeted local checks.
5. Hand off the exact redeploy steps required for Render verification.

## Evidence Collected
- Live `https://integrity-hunting.onrender.com/print` still shows `QR LOAD FAILED` for registration and station QR cards.
- Live `https://integrity-hunting.onrender.com/api/qr?text=test&size=300` returns an image, so the backend QR endpoint is alive.
- The repository and surrounding `CJM GLOBAL` workspace currently contain no `.png`, `.jpg`, `.jpeg`, or `.webp` logo asset at all.
- Frontend code was still pointing directly to `logo.svg`, so even a real PNG could not be used automatically.
- Leaderboard rows were rendered through a helper that searched `document`, but `renderLeaderboard()` called it before the table node had been mounted.

## Fixes Applied
- Frontend QR rendering now fetches backend PNGs, converts them to data URLs, waits for them to load, and only then marks printable QR cards as ready.
- Frontend logo rendering now uses PNG-first fallback logic: it tries `logo.png` first and falls back to `logo.svg` if the PNG file is absent.
- Frontend asset version was bumped to `20260729r5` to force clients to fetch the repaired bundle.
- Backend start validation now blocks the game unless exactly 8 games are configured.
- Backend asset max-age was reduced to `0` to reduce stale frontend caching.
- Leaderboard rendering now writes into the just-created table node immediately instead of waiting for the next realtime tick.
