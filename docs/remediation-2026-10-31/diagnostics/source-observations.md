# Diagnostic page source observations

Checked 2026-10-31 using bounded HTTPS GET requests and an HTML parser that printed only allowlisted row labels, parent table attributes, and boolean support classes. This is source research only; no browser-worker navigation or runtime observation was performed. Sannysoft returned HTTP 403 on one request, then HTTP 200 on a later request.

## Sannysoft

Source: <https://bot.sannysoft.com/>

Current raw primary HTML on 2026-10-31 shows per-row result IDs: `#webdriver-result`, `#advanced-webdriver-result`, `#chrome-result`, `#permissions-result`, `#plugins-length-result`, `#plugins-type-result`, and `#languages-result`. The row labels for Plugins Length and Languages include line breaks before their parenthetical qualifier, so exact full-label text predicates are brittle. Use these exact IDs and ask `/verify` to distinguish result classes or bucket predicates; it enforces a single match and must fail closed to `UNVERIFIED` if the selector is absent or ambiguous. For result classes, test mutually exclusive `#id.passed` and `#id.failed`; for Plugins Length use the exact zero/nonzero decimal regex; for Languages use a blank-cell predicate and locale-shaped text predicate so arbitrary language values are not retained. Raw source has an initial `failed result` class before page scripts update rows, so only the live DOM check can establish the observed outcome.

Permitted low-detail observations for a later bounded run: Advanced WebDriver visible outcome (only after selector proof); plugin length bucket `zero` / `nonzero`; languages `present` / `empty` / `unknown` without retaining values. These are diagnostic observations, not an anti-bot pass.

## BrowserLeaks JavaScript

Source: <https://browserleaks.com/javascript>

The JavaScript Detection table has `id="js-detect"`. Its JavaScript Enabled row has `.true` and `.false` value indicators. Inline Scripts, Same-Origin Scripts, and Third-Party Scripts have value cells `#js-inline`, `#js-self`, and `#js-third`, each using `.true` / `.false` indicators. These remain selector candidates until exact-count-one `/verify` checks succeed. This page also exposes unrelated values; do not collect them.

## BrowserLeaks WebGL

Source: <https://browserleaks.com/webgl>

The safe support rows are `This browser supports WebGL` and `This browser supports WebGL 2`; their value cells have IDs `gl1-status` and `gl2-status` and `.true` / `.false` indicator classes. The page also displays renderer/vendor, hashes, context details, extensions, and limits; none are in scope. These selectors require exact-count-one `/verify` checks on the live page.

## Safe result handling

Treat missing, ambiguous, unknown, and contradictory observations as `UNVERIFIED`; do not coerce them to pass. A challenge/block signal stops the run immediately, with no retry or bypass. A successful diagnostic run is scoped to the named pages and recorded predicates (`DIAGNOSTIC_COMPLETED` / `OBSERVATIONS`), never a global antibot or production-readiness pass. Do not persist page text, raw screenshots, plugin or language values, GPU details, hashes, fonts, user-agent strings, IP, headers, WebRTC, geolocation, or other fingerprint material.
