# Chrome cookie selection integration

The unpacked extension pairs with the local service. The service requests open HTTPS sites, then the operator selects sites in the web panel. Catalog refresh may require the operator to open the extension popup and explicitly approve exact HTTP and HTTPS host permissions for the selected HTTPS sites and eligible parent domains. Chrome checks Secure cookies against HTTPS and non-Secure cookies against HTTP, so both schemes are needed to list the complete catalog. Retry the refresh after granting access. Parent permissions allow browser API access to those parents, even though the catalog filters out sibling-domain cookies. No wildcard subdomain permission is requested; runtime requests remain limited to exact PSL-bounded hosts.

Catalog entries contain metadata rather than values. An export is initiated only after cookie selection; values are retrieved again for the selected identities, and stale selections are rejected. Partitioned cookies are unsupported. The bundled public suffix data needs periodic updates; see [provenance](../chrome_extension/PSL-PROVENANCE.md).

The isolated Chromium test uses synthetic cookies and a temporary profile, and pregrants exact child and parent permissions in a test-only manifest. It checks parent visibility, sibling exclusion, selected-only export, secure and non-Secure cookie metadata, value rotation and deletion, and WebSocket jobs against a local fixture. A separate legacy-permission case confirms that HTTPS-only grants fail closed and list only the missing exact HTTP host permissions. It does not verify Chrome's interactive popup approval or a complete production API-to-Camoufox import. No production cookie values are needed for these tests.

## Incident record: 2026-10-28

An existing Chrome grant covered HTTPS hosts only. Chrome consequently filtered non-Secure cookies from the cookie API result without an explicit error. Extension version 0.1.1 requests exact HTTP and HTTPS permissions for the selected HTTPS site's host and eligible parent domains; the operator grants them from the extension popup. It does not request wildcard subdomains.

After granting access, the fresh Gemini catalog grew from 18 to 28 entries, including newly visible non-Secure `.google.com` cookies named SID, HSID, APISID, and SIDCC. A 16-cookie authentication subset (12 Secure and 4 non-Secure) was imported. The durable worker receipt reported 16 applied and 0 failed. After the final worker restart, a metadata-only snapshot confirmed all 16 entries persisted, unexpired, in the normal unpartitioned cookie context. The session was not re-imported after restart. Google can expire or revoke cookies, so this is not a promise of lasting access.

After restart, an independent read-only control-to-worker verifier confirmed a visible account control and confirmed that ServiceLogin/sign-in links and exact sign-in controls were not visible. Earlier unique-match checks were ambiguous: false could mean zero or multiple candidates. The final check used the first visible match with a broader account-label selector. This independently confirmed signed-in UI state does not establish the account email identity or prove agent task success. Cookie presence and readback do not prove permanent or complete authentication.

Separately, the narrow policy patch for the required Google origins and frame rules is deployed. The previously observed account/`ogs` resource blocks were absent in the current live check; this is a point-in-time observation, not a guarantee against future Google changes.


### Follow-up live verification - 2026-10-28

The earlier waiting task was resumed 4 hours 27 minutes after creation, past its 900-second budget; the backend marked it expired within 32 ms and made no model call. Its earlier attempt had stopped after invalid model key-chord output. Runtime recovery caps invalid key-chord responses at three without dispatching them, and returns `TASK_EXPIRED` on overdue resume so the UI can open Repeat for editing.

A fresh run used the same task specification with a new idempotency key and deadline. The full workflow took 90.857 seconds; its first action started after 2.839 seconds. This is total agent workflow time, not Gemini-only latency. It completed 12 steps with 11 applied actions (2 clicks, 1 text entry, 1 key action, 4 waits, 3 scrolls), then finished as `completed_unverified` because no success criteria were configured. The visible UI showed a model response, with no visible progress indicator or stop control; the worker was idle. Response text was not read, and exact-text correctness remains unverified.

The manual Gemini spinner investigation found main-frame API requests blocked by the resource policy. The narrow fix permits GET, HEAD, and POST to the exact required API origins, plus valid CORS preflight OPTIONS from the canonical Gemini frame only. It does not bypass other host restrictions. The live follow-up found those API blocks absent. This follow-up did not repeat the cookie catalog or import.
