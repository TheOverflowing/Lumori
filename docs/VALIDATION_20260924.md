# Validation — 24 September 2026

| Local complete suite | Research | Product |
| --- | --- | --- |
| Backend | 2654 passed, 19 skipped; 12 subtests passed | 2075 passed, 13 skipped |
| Frontend | 240 passed | 240 passed |

Both backend runs completed without failures. Two dependency deprecation warnings remain per run. Optional runtime/parser checks may skip when dependencies are unavailable. Controlled fixture tests are not real-provider quality, human review or production-performance evidence.

118 application files match across the repositories; SHA-256 manifests are retained. Changed Python files compile and changed tracked files pass whitespace checks. Candidate files were checked against local credential values and common credential signatures; the only signature match was an existing artificial credential in a test. No actual secret match or file over 95 MiB was found. Personal resume files are ignored and not included.

The old CA1 GitHub checks were confirmed successful before this update. This machine's Docker daemon is unavailable; the product GitHub workflow will build the full image and test startup, registration and persistence after push. New remote CI status is external to this immutable record and must be checked separately. No new paid provider experiments or document rendering were performed for this release.
