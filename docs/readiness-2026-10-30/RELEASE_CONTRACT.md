# Release contract (2026-10-30)

## Release decision

Production remains **NO-GO**. This contract records the currently evidenced product boundary and the conditions for a later release decision. It does not authorize deployment or change the production profile. The accepted model protocol for a candidate is `generic` against the configured model service at `http://host.docker.internal:8080/v1` (port 8080 is the model API; 8081 is not the service endpoint). The currently tested Fara path remains unverified and must not be described as release-ready.

## Intended service behavior

The service accepts an explicit user task, restricts navigation to the task's `allowed_origins`, proposes normalized browser actions, executes those actions through one browser worker, and retains task/action state for status, verification, cancellation, and operator review. The operator UI, API/MCP access controls, and worker authentication remain part of the service contract. A task must not be reported successful without a verified goal effect; uncertain dispatch or an intercepted action must remain recoverable or require human review.

The browser invariant for this release is **one worker, one active autonomous task, one browser context and one page**. A concurrent task must be rejected or remain queued; it must not create another worker, context, or page. Task identity, allowed-origin checks, action history, and operator authentication are not waived by this bounded release scope. Authenticated websites and cookie/session use remain subject to explicit task authorization and the service's existing account identity and origin policies; there is no evidence here for broad account-session compatibility or cross-account workflows.

## Verified user-facing browser scope

The live disposable DemoQA run exercised eight scenarios: `text_box`, `invalid_email`, `checkbox`, `web_table`, `radio`, `double_click`, `dynamic`, `notify_then_click`. The full G3 run passed 8/8 with an independent verifier, and checkbox/web_table each passed three repeat runs. HTTPS navigation to the allowlisted DemoQA origin was exercised. These results establish bounded test evidence, not a general success rate or latency SLO.

Generic-protocol observation hints are an explicitly opt-in feature. They remain off by default and are not deployed. A candidate may set `BROWSER_OBSERVATION_HINTS=1` only after selecting `MODEL_PROTOCOL=generic`; the verified G3 exercised that combination in the disposable stack. Fara protocol must keep hints off.

## Explicit capability boundary

The following remain unsupported or unverified for release claims: file upload, native browser dialogs, frame-targeted actions, multiple windows/tabs, arbitrary authenticated-site workflows, and broad compatibility with anti-bot challenges. An iframe was exercised only by a synthetic worker fixture; that does not establish frame-targeting support. Overlay interception was shown to avoid a false success and duplicate click, but broad overlay resolution is not established. Do not claim these capabilities from the current evidence.

## Release gates

A future GO requires a release-owner decision against this contract and evidence for the selected deployment configuration. At minimum:

- generic model and the exact alias/endpoint are verified; feature flags and model protocol are recorded;
- the candidate has repeatable live browser results with independent effect verification;
- task lifecycle evidence covers uncertain replies, restart without redispatch, concurrent-task exclusion, cancel, and operator review;
- any capacity/SLO claim has a bounded soak with complete browser memory, PID, and OOM evidence; missing resource counters are a failed evidence gate, not zero;
- database/schema compatibility evidence exists before changing storage schema;
- production profile and task state are preserved, and the current single-worker handoff/restore procedure is followed.

A release may narrow its declared capability set only through a documented release-owner decision. This contract does not silently remove existing service features or authorize a change to authentication/session policy.
