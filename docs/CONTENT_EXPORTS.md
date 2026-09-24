# Content ratings and downloads

Implemented 2026-09-20. This feature does not call generation or embedding providers.

## Course library

**Content and review** follows the selected course. Its records show the matching material icon and a Lesson, Quiz or Assignment label. Status filters, title search, pending-review counts, and running or failed jobs are scoped to that course. Switching courses resets the status filter to **All** and clears the search. Opening or exporting a material still uses that material’s saved content and version.

## Ratings

`GET /api/contents/{cid}` returns `evaluation` for the current content version, or `null`. The UI defaults to a collapsed **Content rating** section. Reopening a saved rating restores all scores, notes and question judgments.

`POST /api/contents/{cid}/evaluations` requires the current content version. The first save returns HTTP 201; subsequent saves update the same evaluation ID and return HTTP 200. A SQLite transaction checks the version again before writing. Changed metrics preserve the previous snapshot in `evaluation_history`; an identical retry does not add an audit snapshot. The `current_evaluations` table points to one observation for each content/version. Historical duplicate evaluations remain intact; the most recent one initializes the current view. CSV exports choose only that current observation for each version to avoid duplicate sample counts.

Content edits create a new version and start a separate rating. Ratings are optional quality feedback; they do not train the model, automatically revise content, prove factual correctness, or measure student learning outcomes.

## Documents

`GET /api/contents/{cid}/export?format=pdf&include_answers=false&language=en&version=1`

- `format`: `pdf`, `docx`, or legacy `md` (default).
- `include_answers`: whether to include question answers and explanations (default true).
- `language`: `en` or `zh`; labels only, authored text is not translated.
- `version`: optional expected current version. The UI always supplies it; a mismatch returns HTTP 409.

New quizzes and assignments default to questions without introductory learning objectives or teaching sections. The generation form’s **Include an introduction** switch (`include_explanations`) opts into that teaching content; it does not remove conditions or data from question stems, or disable answers and their explanations. Lessons keep their teaching content. This generation choice does not rewrite older materials.

PDF and DOCX render the saved version in A4 layouts, including any existing learning objectives and sections, question options, optional answers, and numbered source references. Exports neither add an introduction to question-only content nor remove existing teaching sections from older content. The export option `include_answers` controls answers and answer explanations independently of the generation-time introduction switch. Export numbers identify documents: all cited chunks from one document share one number and one bibliography entry with combined page locations. The review page retains its passage-level references for source inspection, so its numbering may differ. Stored content and raw JSON evidence remain unchanged. Supported reference notation is converted; code spans remain literal. Source names and available external provenance are retained. Exports render stored text; they do not insert generated audio/video or convert arbitrary LaTeX/Markdown into full mathematical typesetting. Markdown remains a compatible evidence-oriented text export.

Document identity uses the explicit document ID first; only sources without one can fall back to the same normalized safe external URL. Equal filenames alone never merge documents. Export numbers include only cited groups; answer-only sources are omitted when answers are excluded. Word keeps each bibliography entry together.

The review toolbar groups **Export** and, for approved content, **Open learning view** as matching secondary controls. The evidence JSON link is inside **Export → More exports**, collapsed by default.

Production dependencies are ReportLab and python-docx, pinned together with their dependencies in `requirements.lock.txt`. The app creates exports in memory and returns an attachment with a UTF-8 title/version filename. Deploy the complete `app` directory, including bundled licensed fonts. No LibreOffice process or Codex runtime is required by the application; LibreOffice and Poppler are used only in development to inspect output.

## Media

`GET /api/media/{mid}/file` remains an inline preview. `GET /api/media/{mid}/download` returns the stored media as an attachment with its MIME type and filename. Both enforce content ownership and resolve the path inside the configured media directory; missing files and path escapes return 404.

The download route is independent of the generation provider and accepts persisted audio, image and future video entries. Audio/image downloads are exposed beside their previews. The UI can show a video player and download action once a video entry exists, while video generation remains unavailable until an actual provider is connected. This is an extension point, not a claim of completed video generation.

All routes require the owning account. Binary document downloads use the existing session-aware client, which aborts or discards responses after an account switch. Errors leave the export dialog available for retry. Closing the export dialog cancels its request and suppresses a late attachment. Stored media uses native browser attachment downloads so long files retain browser transfer controls and range support instead of first buffering into JavaScript.

## Verification

See [initial rating/export QA](https://github.com/TheOverflowing/lumori-fyp/blob/snapshot-2026-09-24/docs/ui/review-exports-20260920/README.md) and [document grouping and toolbar QA](https://github.com/TheOverflowing/lumori-fyp/blob/snapshot-2026-09-24/docs/ui/difficulty-export-polish-20260920/README.md). Automated tests and synthetic fixtures are separate from real provider runs and user ratings.
