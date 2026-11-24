# Sheet templates

A template describes an answer-sheet layout so sheets can be read locally,
without an AI call per sheet.

1. Upload a reference image with `POST /api/file/image/`. Prefer a blank sheet.
2. Create a template with `POST /api/app/sheet-templates/`, sending `name` and
   `image_id`.
3. Pass the template's `id` as `template` when creating a batch with
   `POST /api/app/answer-sheet-batch/`.

The sheet needs four corner markers, an answer grid, and a roll-number digit
grid with ten bubbles per digit. A registration grid is optional. Templates
show their detected question count, option counts, numbering, and digit counts
in `structure_summary`. The summary also includes the aligned canvas dimensions
and grid bounding boxes in `[x0, y0, x1, y1]` coordinates. Older templates
without grid metadata return an empty `grids` object. If numbering was detected
incorrectly, update it with
`PATCH /api/app/sheet-templates/{id}/` and `numbering: "column"` or `"row"`.
A template used by a batch cannot be changed or deleted. Sheets that do not fit
the template go to review as `layout_mismatch`.

## AI and privacy

AI is used to create or replace a template and to extract an answer key. Batch
sheet reading uses local OpenCV processing. During template creation, OpenCV
detects bubble geometry and counts; AI only classifies detected groups,
question numbering, and digit order. Answer choices are labeled left to right
as A, B, C, and so on, so non-standard printed option orders are not supported.

Configure `COARENA_API_KEY`, `COARENA_BASE_URL`, `COARENA_MODEL`,
`COARENA_MAX_TOKENS`, and `COARENA_REASONING_EFFORT`. The default model is
`deepseek-v4-1-flash-1017`; Coarena's catalog lists it as vision-capable with
base64 image support. `COARENA_MAX_TOKENS` defaults to `16384`. The reasoning
effort is omitted when empty; for the default model, `none` is supported. Use
only values supported by the selected model.

Coarena stores requests and responses and may use them in datasets it licenses.
Template creation sends an aligned image with pixels outside detected bubble
regions blanked, though nearby handwriting may remain. Answer-key extraction
sends the full uploaded image. Prefer blank reference sheets and do not send
images containing personal information unless your organization is authorized
to do so.

## Checks before scanning

- Scan a sheet with known answers to verify the detected question numbering
  before processing a real batch; numbering can be corrected on the template.
- If clean sheets are flagged because printed marks look faint, tune the mark
  thresholds in `omr.decode_marks`.
- If registration bubbles are printed on a sheet but unused, leave them blank;
  partially filled or ambiguous registration marks require review.
