# Sannysoft initialization review

Read-only source/runtime review after diagnostic runs 1 and 2. No additional navigation was performed for this review.

## Evidence

- The public Sannysoft HTML loads `lodash.min.js` and `jquery.min.js` from `cdnjs.cloudflare.com`, and `ua-parser.min.js` from `cdn.jsdelivr.net`; it also loads three scripts from `bot.sannysoft.com`.
- The same-origin `fpScanner.js` source references the `UAParser` global supplied by the jsDelivr asset.
- All seven result elements (`webdriver-result`, `advanced-webdriver-result`, `chrome-result`, `permissions-result`, `plugins-length-result`, `plugins-type-result`, `languages-result`) start in raw HTML with class `failed result` and empty result text.
- The large inline test script references all seven IDs and mutates result DOM. It uses async/await, with no 15-second timer or explicit completion marker found in the source.
- The idle worker status on Sannysoft reported script-origin policy blocks: four requests from cdnjs and two from jsDelivr, all `origin_not_allowed`.

## Interpretation

The first two Sannysoft receipts remain historical observations, but their result values are not confirmed as initialized test outcomes. In particular, a `failed` class and an empty Languages cell match raw source defaults. The WebDriver and Chrome strings remain recorded; source/runtime evidence was not sufficient to elevate them to a completed Sannysoft observation while required page scripts were blocked.

Run 3 used a narrow Sannysoft-only static resource allowlist for the exact public page origin plus the two source-confirmed script origins. It kept `allow_cross_origin_navigation=False`; BrowserLeaks remained restricted to its own origin. The post-run worker status reported no blocked scripts from either required CDN origin. Each result class agreed with a non-empty visible result value (except the Languages empty/present distinction was resolved only after the other six outcome rows resolved), supporting the scoped `DIAGNOSTIC_COMPLETED` label in [run-3.json](run-3.json). The failed Chrome row is preserved as a diagnostic outcome; no fingerprint setting was changed.
