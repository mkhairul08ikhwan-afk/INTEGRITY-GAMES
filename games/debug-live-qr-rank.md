# [OPEN] live-qr-rank

## Symptoms
- QR on registration and print pages fails to render.
- Logo does not match expected source image.
- Coach can proceed without mandatory 8 games.
- Leaderboard/ranking does not reflect expected live event state.

## Hypotheses
1. The deployed frontend is stale and not serving the newest `app.js` / `index.html`.
2. The local QR library is not available at runtime, so QR render falls back to failure UI.
3. The coach and leaderboard views are not re-rendering correctly from incoming live state.
4. The mandatory 8-game rule is bypassed by frontend logic or an older deployed build.
5. The logo currently served is not the original asset, so distortion/wrong-image reports are expected.

## Evidence Plan
- Inspect live HTML and asset bundle contents.
- Instrument frontend runtime around QR render, route rendering, and live state updates.
- Instrument backend request paths for public state, registration, start, leaderboard, and assets.
- Reproduce locally where possible and compare with deployed behavior.

## Evidence
- Live deployed `app.js` includes exact-8 UI gating (`count !== max`) and QR render path using `window.QRCode.CorrectLevel.H`.
- Live deployed `vendor/qrcode.js` is served successfully.
- The bundled QR library assignment order can leave `QRCode.CorrectLevel` undefined after reassignment.
- Repository contains only `frontend/logo.svg`; no original PNG/JPG/WebP logo asset exists.

## Hypothesis Status
1. Stale frontend deployment: INCONCLUSIVE
2. QR runtime failure from frontend QR handoff: CONFIRMED
3. Coach/leaderboard re-render timing issue: INCONCLUSIVE
4. Exact-8 rule bypassed by current local code: REJECTED
5. Wrong logo source asset in repository: CONFIRMED
