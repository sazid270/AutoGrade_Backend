# OMR grading API

All endpoints are under `/api/omr/`, need `Authorization: Bearer <jwt>`, and
only ever expose the caller's own data (anything else is a 404).
Errors look like `{"error": "message"}`.

## Flow

1. Upload each sheet image with `POST /api/file/file/` (multipart field `file`) → keep its `id`.
2. `POST assessments/` with the answer key and the file ids → `202 {assessment_id, job_id, sheets_queued, duplicates, missing_file_ids}`.
3. Poll `GET jobs/{job_id}/` (~1s) until `status` is `done` or `error`. `stages` holds the 9 stages with `status/ms/detail`.
4. `GET assessments/{id}/` lists every sheet with its score; open flagged ones with `GET sheets/{id}/`.
5. Resolve flagged questions with `PATCH sheets/{id}/verify/`.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `assessments/` | Your assessments with counts (sheets, processed, rejected, pending, needs_review) |
| POST | `assessments/` | Create + start grading. Body: `title, subject, class_name, exam_date, assessment_type, answer_key {"1":"A",...}, correct_marks, wrong_marks, blank_marks, file_ids[]` (≤ 40) |
| GET | `assessments/{id}/` | Detail: key, mark policy, `latest_job`, `sheets[]` |
| PATCH | `assessments/{id}/` | Edit metadata. Changing `answer_key` or marks **rescores every sheet instantly** (no image re-read). 409 while a job runs |
| DELETE | `assessments/{id}/` | Delete (409 while a job runs) |
| POST | `assessments/{id}/sheets/` | Grade more sheets later: `{file_ids: []}`. Re-uploads of the same image are skipped |
| POST | `assessments/{id}/reprocess/` | Retry rejected sheets. `{"include_processed": true}` re-reads everything (drops verifications); `sheet_ids` narrows it |
| GET | `assessments/{id}/analytics/` | Average/high/low, pass rate, topper, per-question difficulty + option picks, grade distribution, histogram |
| GET | `assessments/{id}/export/` | CSV: one row per sheet, one column per question |
| GET | `jobs/{id}/` | Job progress |
| GET | `sheets/{id}/` | Sheet + `image_url` + `per_question` (options with fill ratios, flags, base64 `crop` of flagged questions) |
| PATCH | `sheets/{id}/` | Fix the roll number: `{roll_no}` |
| DELETE | `sheets/{id}/` | Remove a sheet |
| PATCH | `sheets/{id}/verify/` | `{question_no, selected: "B" | null}` → sheet rescored and returned |

## Review flags

`multi_mark` (more than one filled), `ambiguous` (partly filled), `low_confidence`,
`missing_bubbles` (fewer options found than the key needs), `not_detected`
(a keyed question the reader could not find; scored as blank until verified).

## Sheets the reader handles

Flat scans and phone photos (page found and perspective-corrected, deskew up to
±6°, any resolution). The bubble grid is located from the print itself, so no
fixed template is needed; the roll number is read from a 0–9 digit grid when
one is filled in. Rejected sheets carry a `reject_reason` the teacher can act on
(blurry, not an OMR sheet, PDF, too small...).

## Operations

Jobs run on a background thread inside the web process (no broker needed).
If the server restarts mid-job, the job is marked `error` after
`OMR_JOB_STALE_SECONDS` (default 600) of silence; use `reprocess` to resume.
Tests set `OMR_RUN_JOBS_IN_THREAD = False` to run jobs inline.

Run the tests: `python manage.py test omr`
