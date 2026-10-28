# Public Suffix List snapshot

`public_suffix_list.dat` was retrieved from the official https://publicsuffix.org/list/public_suffix_list.dat on 2026-10-27. Its header identifies version `2026-10-22_08-36-41_UTC` and upstream commit `8437e5262f13305f94087abe699b4fe2be1d1de8`. SHA-256 of the file is `0232f4df0831911356e8ee1f4d5c3b6b12c0ed3f6bd66b0f81c8ab5d3d49e9ca`. The list is licensed under MPL 2.0, as stated in its header.

All 10,397 nonblank, non-comment rules match the pinned upstream commit's `public_suffix_list.dat`; the normalized rule lines have SHA-256 `c790840c257cd151caa35987344a5864b9b50f5729ec379d45c128d7ad7b2487`. The official served file includes version and commit header lines absent from the raw source, so their full-file hashes differ. `public_suffix_rules.js` is generated from those nonblank, non-comment lines as a JSON array and contains both ICANN and private rules.

Refresh this snapshot periodically from the official URL. Record the version, commit and hash, regenerate the JavaScript array, and run the extension contract and isolated Chromium tests. Changes to private and ICANN suffixes can change permission boundaries.
