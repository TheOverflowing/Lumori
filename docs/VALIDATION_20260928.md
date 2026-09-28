# Validation — 28 September 2026

This application sync contains development through 26 September: browser drafts and reading navigation, optional Harness flow extensions, isolated question recovery and the disabled-by-default review_v2 observer. Deployment configuration is preserved.

## Local checks

| Check | Result |
| --- | --- |
| Initial full backend suite | 2,267 passed, 1 failed, 16 skipped; 407.93 seconds |
| Correction and focused rerun | 8 passed across content-library and observer API tests |
| Frontend suite | 291 passed, 0 failed |
| Python compilation | Application and product scripts passed |
| Application parity | 133 files match the research checkout byte-for-byte |
| Whitespace and updated README links | Passed |

The initial failure was an outdated exact-field assertion for job details. The API now includes `difficulty_shadow`; the test was updated to require this field and verify it is `None` when the observer is off. The application was not changed by this correction. The full suite was not repeated after this test-only update; the focused rerun verifies the affected behavior. Two existing dependency deprecation warnings remain. Optional Harness runtime integrations were not enabled.

The tests ran in the isolated product checkout with the research Python 3.12 environment and a copy of the existing ignored tokenizer assets. No environment secrets or user databases were copied. Tests use controlled transports and temporary data; they do not measure model quality or live-provider availability.

```sh
PYTHONPATH=. /path/to/python -m pytest -q
PYTHONPATH=. /path/to/python -m pytest -q tests/test_content_library.py tests/test_difficulty_shadow_api.py
node --test tests/frontend_*.test.mjs
/path/to/python -m compileall -q app scripts
git diff --check
```

## Deployment and CI

Dockerfile, Compose configuration, model manifest and container smoke script were preserved. The local Docker daemon was unavailable, so no new local container build or smoke test is claimed. Check [GitHub Actions](https://github.com/TheOverflowing/Lumori/actions) for the runs attached to the published commit; the previous v0.3.0 checks passed but do not validate this update.

Default runtime remains native. Harness requires separate installation. The new observer defaults to off and never changes primary acceptance decisions. Publication of source does not update or restart an already-running deployment.
