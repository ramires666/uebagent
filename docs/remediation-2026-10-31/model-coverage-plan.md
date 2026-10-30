# P1 model coverage: two read-only public scenarios

The earlier model-driven DemoQA check passed but exercised one site and one very short navigation path. The readiness question is broader: confirm that the model can follow a bounded public UI task on two different sites while an independent verifier observes intermediate and final state. This plan adds exactly two scenarios; it does not alter the old helper or task prompts.

## Scenario bounds

| Scenario | Allowed site and path | Independent evidence | Guardrails |
|---|---|---|---|
| DemoQA Check Box | DemoQA home → visible Elements → Check Box; read labels and leave the tree unchanged | Exact Check Box URL and visible `Check Box` / `Home` text; Home checkbox remains unchecked; no selected-result text; task success phase and verified result; at least two observed clicks in sanitized action trace | No checkbox selection, forms, or sign in; stop on challenge |
| eBay guest search | eBay home → exact signed-out proof → navigate once to the fixed public search URL for `notebook` → public results → one item page → history back | `Sign in or register` and home URL observed together before search; expected results URL and public result text; independent `/itm/` URL plus visible product heading; observed history-back and final results page; task success phase and verified result | No login, typing, cart, watchlist, or purchase; missing signed-out marker cancels before search; missing item evidence cannot pass; stop immediately on challenge; no retry |

Both tasks cap execution at 16 actions and 420 seconds. A missing milestone or unobserved signed-out state cannot pass. The runner requires the disposable `APP_ENV=test`, `READINESS_DISPOSABLE=1`, exact control origin `http://localhost:18081`, and the existing exact worker, assignment, route, and one-context/one-page idle guards. It refuses to overlap an active task, uses MCP for task lifecycle, and cleanup is scoped to the task ID it created. Challenge detection cancels the owned task before further checks. Reports contain only bounded status, verifier booleans, safe error codes, and action kinds; they omit model raw output, credentials, cookies, IPs, and page captures.

Amazon is excluded because its P1 visit stopped at an access/continue-shopping interstitial. This is a bounded coverage exercise, not a general anti-bot or production-readiness claim. Execute the two scenarios serially after the separately owned browser work is complete; stop the sequence on a challenge or worker guard failure.

## Offline acceptance

`tests/test_readiness_model_coverage.py` covers cross-site scenario registration, DemoQA non-selection constraints, home-and-marker pre-search gating, rejection when the marker appears only after search/back, mandatory item-page evidence and history-back trace, write-action rejection, challenge cancellation, verified phase, independent milestone/final checks, wrong-route failure, and unknown-error failure. These tests do not contact the sites or launch model tasks.
