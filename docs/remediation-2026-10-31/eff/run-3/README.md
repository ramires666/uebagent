# EFF origin remediation run 3

## Finding and change

The prior P2 EFF run ended at `/kcarter`. A sanitized status probe showed one `origin_not_allowed` resource from `https://firstpartysimulator.net`. The EFF-maintained source confirms an ordered first-party redirect cycle across `coveryourtracks.eff.org`, `firstpartysimulator.net`, and `firstpartysimulator.org`, plus three configured tracker iframes. Because the CTA was selected with `aat=1`, the EFF results template also conditionally requests `https://media.net/favicon.ico`.

The corrective harness now supplies those seven exact HTTPS origins as a per-action allowlist and sets `allow_cross_origin_navigation=false`. The worker's global route policy is unchanged. Result completion requires the results page, a results heading, all three summary labels, a populated summary sentence, and one recognized answer for each of the ad, tracker, and fingerprint rows. It records only booleans and blocked-reason counts.

Primary sources: [EFF `config_example.py`](https://github.com/EFForg/cover-your-tracks/blob/master/config_example.py), [EFF `main.py` first-party route](https://github.com/EFForg/cover-your-tracks/blob/master/main.py), [EFF `results.html`](https://github.com/EFForg/cover-your-tracks/blob/master/templates/results.html), and [EFF result answer templates](https://github.com/EFForg/cover-your-tracks/tree/master/templates).

## Run result

One CTA click completed in 25.048 seconds. The page reached `/results`; both result heading predicates, all three labels, the populated summary sentence, and all three recognized answer predicates were true. No challenge text or blocked resource was observed. The human gate remained held with the manual cross-origin bypass disabled and was released afterward. No account credentials were submitted and no cookies were imported.

The final sanitized worker state is in [post-run-state.json](post-run-state.json); run metadata is in [metadata.json](metadata.json). The temporary screenshot used to reconfirm the CTA location was deleted after visual review.

This is a **functional EFF diagnostic completion** result only. The test allowlist includes EFF's tracker endpoints so the diagnostic can finish; this run does not measure network tracker blocking or establish a privacy grade. The original published P1/P2 artifacts remain unchanged.
