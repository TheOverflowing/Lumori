# Changelog

## v0.3.0 — 2026-09-24

- Add cancellation/recovery, passed partial results and version-checked question revision.
- Sync generation quality gates, resource limits, hybrid exploration and frontend performance updates.
- Preserve deployment settings and update the container smoke assertion for readiness-aware health responses.
- Research snapshot: `snapshot-2026-09-24`; thesis and experiments remain in the research repository.

## v0.2.0-ca1 — 2026-09-20

- Sync the CA1 application from research tag `ca1-2026-09-20`: per-question generation/review, continuation, difficulty acceptance, query fusion/clarification, bounded source exploration, document lifecycle, ratings and Word/PDF exports.
- Preserve deployment host restrictions, `/healthz`, model preparation, Docker/Compose and data persistence.
- Include product regression tests and font licenses; keep research runs, papers and thesis deliverables in `lumori-fyp`.
- Update provider-budget and quality-retry fixtures to match current behavior. Optional Harness runtime remains separately installed.


## v0.1.0 — 2026-09-18

Initial standalone deployment repository, extracted from research checkpoint `TheOverflowing/lumori-fyp@3e61644a251e9e88e450463ebd57ea0d28c41238`.

Includes the application, frontend, authentication and account isolation, RAG generation, MinerU/Docling document pipeline, portable deployment configuration, pinned model download manifests, and regression tests. Adds configurable allowed hosts and a non-sensitive `/healthz` endpoint for deployment checks. Research archives and local user data are excluded.
