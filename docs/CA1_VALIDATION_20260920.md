# CA1 validation — 2026-09-20

## Local evidence

| Check | Research repository | Product repository |
| --- | --- | --- |
| Frontend complete suite | 219 passed | 219 passed |
| Backend full sweep before final fixture correction | 2444 passed, 19 skipped, 1 failed; 12 subtests passed | 1903 passed, 13 skipped, 1 failed |
| Follow-up on corrected text fixture, schema repair, provider budget and deployment tests | 67 passed | 67 passed |

The single failure in both final full sweeps was the same long-text fixture: it returned explanatory sections without requesting `include_explanations`. The request now explicitly enables that feature; the regression passes. The full suite was not repeated after this fixture-only correction. The 67 follow-up cases overlap the full sweep and must not be added as distinct test coverage.

Earlier checks identified stale schema-repair assertions and a test that bypassed provider metering. Fixtures now assert three quality attempts and inject malformed JSON through the HTTP fixture, retaining provider budget enforcement. No production retry policy was changed. Product-specific host restrictions and `/healthz` were retained and synchronized to the research source.

Optional parser/harness tests are skipped when their separate dependencies are unavailable. Existing FastAPI/Starlette deprecation warnings remain. These are offline tests with controlled model responses, not paid-provider verification, teacher review, learner results or measured production performance.

## Packaging checks

- 113 application files have matching SHA-256 hashes across both repositories (see application manifest).
- Existing local secret values and common credential signatures were scanned. Matches were synthetic test strings/test names; no actual credential match was found in candidate files. This is a scoped scan, not a guarantee about every possible secret format.
- `.env`, account/runtime databases, model weights and virtual environments are excluded. The two target repositories were confirmed private through GitHub metadata.
- Updated research payload before compression is about 262 MiB; no candidate file exceeds GitHub's 100 MiB file limit.
- Current thesis v0.4 Word/PDF and historical artifacts were archived as existing files; no document re-render or new content review was performed.
- Local Docker daemon was unavailable. The existing GitHub product workflow builds the full image and checks service persistence after push; its outcome must be checked separately.

Raw local test logs are retained alongside this record in the research repository. CI run status is external to this immutable snapshot.
