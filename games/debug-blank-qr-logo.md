# Debug Session: blank-qr-logo
- **Status**: [OPEN]
- **Issue**: All QR codes render as blank white squares and the CJM Global logo is visually distorted.
- **Debug Server**: Pending startup
- **Log File**: .dbg/trae-debug-log-blank-qr-logo.ndjson

## Reproduction Steps
1. Open Coach Dashboard.
2. Navigate to Registration and Printable QR page.
3. Observe QR canvases rendering as white squares without visible QR modules.
4. Observe header/welcome logo rendering with incorrect proportions.

## Hypotheses & Verification
| ID | Hypothesis | Likelihood | Effort | Evidence |
|----|------------|------------|--------|----------|
| A | QR library fails to load at runtime, leaving canvases blank. | High | Low | Confirmed: frontend used `window.QRCode` from external CDN and left empty canvases in place when the global was missing. |
| B | QR generation runs before the canvas or data is ready, so the canvas is never painted correctly. | Medium | Medium | Rejected: QR render calls happened after DOM creation; failure mode matched missing renderer, not timing. |
| C | CDN dependency is blocked or incompatible in deployed/runtime environments, so QR rendering is unavailable. | High | Low | Confirmed: `frontend/index.html` loaded QR generation from jsDelivr only, while deployed symptom was blank canvases. |
| D | The current `logo.svg` is not the original logo asset, so the visual mismatch is asset-level, not only CSS. | High | Low | Partially confirmed: repository only contained a recreated `logo.svg`, not the original uploaded raster asset. |
| E | CSS sizing rules distort the logo by forcing dimensions that do not match the source aspect ratio. | Medium | Low | Confirmed: header logo used fixed width+height, which risks forced box sizing for a complex crest. |

## Log Evidence
- Static audit evidence:
  - `frontend/index.html` loaded `https://cdn.jsdelivr.net/npm/qrcode@1.5.4/build/qrcode.min.js`
  - `frontend/app.js` attempted `window.QRCode.toCanvas(...)` but left the canvas intact when `window.QRCode` was absent
  - `frontend/styles.css` fixed `.brand-mark` to `52px x 52px`
  - `frontend/logo.svg` was the only logo asset in the repository

## Verification Conclusion
- QR generation moved to same-origin backend SVG rendering via `/api/qr`, removing CDN dependency and blank-canvas failure mode.
- Logo rendering now keeps natural aspect ratio in both header and hero placements.
