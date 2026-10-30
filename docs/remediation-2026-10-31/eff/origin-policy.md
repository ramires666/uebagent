# EFF remediation origin policy

The remediation harness grants a per-action allowlist for seven exact HTTPS origins. The list is scoped to this one public Cover Your Tracks diagnostic and never changes the worker's global routing policy.

| Origin | Why this diagnostic needs it | Primary EFF source |
|---|---|---|
| `https://coveryourtracks.eff.org` | Entry page, return redirect, results and same-origin assets | [EFF `config_example.py`](https://github.com/EFForg/cover-your-tracks/blob/master/config_example.py), [EFF `main.py`](https://github.com/EFForg/cover-your-tracks/blob/master/main.py) |
| `https://firstpartysimulator.net` | First-party redirect cycle and fingerprint check | [EFF `config_example.py`](https://github.com/EFForg/cover-your-tracks/blob/master/config_example.py), [EFF `main.py` `/kcarter`](https://github.com/EFForg/cover-your-tracks/blob/master/main.py) |
| `https://firstpartysimulator.org` | Second simulator hop in the configured first-party cycle | [EFF `config_example.py`](https://github.com/EFForg/cover-your-tracks/blob/master/config_example.py) |
| `https://trackersimulator.org` | Configured ad-server tracker iframe and result tally | [EFF `config_example.py`](https://github.com/EFForg/cover-your-tracks/blob/master/config_example.py), [EFF `results.html`](https://github.com/EFForg/cover-your-tracks/blob/master/templates/results.html) |
| `https://eviltracker.net` | Configured tracker-server iframe and result tally | [EFF `config_example.py`](https://github.com/EFForg/cover-your-tracks/blob/master/config_example.py), [EFF `results.html`](https://github.com/EFForg/cover-your-tracks/blob/master/templates/results.html) |
| `https://do-not-tracker.org` | Configured DNT-server iframe and result tally | [EFF `config_example.py`](https://github.com/EFForg/cover-your-tracks/blob/master/config_example.py), [EFF `results.html`](https://github.com/EFForg/cover-your-tracks/blob/master/templates/results.html) |
| `https://media.net` | EFF results template conditionally requests `/favicon.ico` when the CTA carries `aat=1` | [EFF `results.html`](https://github.com/EFForg/cover-your-tracks/blob/master/templates/results.html) |

The previous P2 run's sanitized worker status had one blocked resource from `https://firstpartysimulator.net` with `origin_not_allowed`, while the page later settled at `/kcarter`. This matches the official configured redirect cycle. The allowlist therefore includes the exact EFF test graph and uses `allow_cross_origin_navigation=false`; no arbitrary cross-origin permission is enabled.

The test also allows the three configured tracker endpoints so the public diagnostic can complete its own tally. Consequently, this run can establish functional completion of the EFF diagnostic only. It cannot be reported as a network tracker-blocking or privacy grade. Results are retained as booleans indicating that each known result row has one recognized answer; answer text, query strings, cookies, fingerprints, and IPs are not retained.
