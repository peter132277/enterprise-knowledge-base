# Adaptive local processing

Choose the cheapest available host method that preserves all material information. Do not equate “complete processing” with “render every page”, and do not install a converter, OCR engine, transcription model, or managed runtime from this Skill.

## Decision sequence

1. Inspect container structure and metadata.
2. Detect text coverage, tables, images, charts, comments, revisions, formulas, embedded files and encryption.
3. Extract complete machine-readable content once.
4. Use visual rendering only where layout or visual evidence carries information.
5. For large sources, create a full local extraction and analyze it in bounded chunks.

Write one complete UTF-8 Markdown extraction under `.kb/temp`. Preserve source facts, model descriptions and model inferences separately, and include page, structure, region, or timestamp locators. Record the extraction hash plus this manifest profile:

```json
{
  "source_kind": "pdf",
  "method": "native-text",
  "provider": "bundled-runtime",
  "locator_type": "page",
  "units_total": 24,
  "units_processed": 24,
  "complete": true,
  "review_required": false,
  "warnings": []
}
```

The transaction rejects missing providers, empty extraction, incomplete unit coverage, invalid locators, changed extraction hashes, or credential material found only after binary extraction.

## Documents

- DOCX/ODT/RTF:
  - use the available host document-reading capability or bundled runtime; do not copy an Office parser into this Skill;
  - inspect paragraphs, styles, tables, media relationships, headers/footers, comments and revisions first;
  - pure text with no meaningful visual objects: extract text and headings directly; do not export hundreds of pages solely for visual inspection;
  - tables, diagrams, floating shapes or layout-sensitive pages: render affected sections;
  - uncertain structure: sample beginning, middle, end and anomaly sections before deciding on wider rendering.
- PDF:
  - use the available host PDF capability; prefer native text extraction before rendering;
  - text PDF with high text coverage: extract text and inspect representative/anomalous pages;
  - scanned PDF: OCR all pages;
  - mixed PDF: OCR only image/scanned pages and inspect pages containing charts or diagrams.
- PPT:
  - inspect every slide visually because layout is semantic;
  - extract slide text, notes, charts and slide numbers.

## Spreadsheets

- Preserve workbook and formulas.
- Inspect every sheet’s dimensions, headers, types, merged cells, formulas and error cells.
- Analyze large sheets with statistics and deterministic samples; do not paste all rows into Markdown.
- Preserve sheet and cell-range locators.
- Render only charts, dashboards or formatting-dependent regions.

## Images

- Use host vision for OCR and visual interpretation. Process each source image; use `locator_type: region`.
- Mark unclear text as `[无法确认]`.
- Preserve the original and identify the relevant image region.

## Audio and video

- Accept only `feishu-minutes` or `local-whisper-cpp`. Never use Tencent Cloud, a shared credential, or an automatic fallback.
- Default Feishu path: require the router-prepared canonical transcript and profile produced after its separate upload preview and confirmation. This Skill must not call Feishu itself. Preserve `source_file_url` as source-note Frontmatter `source_feishu_file_url`; the transaction requires the two values to match.
- Offline local path: use only when the user explicitly requests local or offline processing. Do not install or download a binary, model, FFmpeg, Python environment, or GPU runtime. Invoke the existing tools in one foreground process:

```powershell
python <skill-root>/scripts/prepare_media_transcript.py local-transcribe `
  --vault . `
  --source "<absolute-media-path>" `
  --output ".kb\temp\<name>.transcript.md" `
  --profile-output ".kb\temp\<name>.profile.json" `
  --allow-local-execution
```

- The local adapter requires an existing `whisper-cli`, multilingual model, `ffmpeg`, and `ffprobe`; paths may come from `WHISPER_CPP_BIN`, `WHISPER_CPP_MODEL`, `FFMPEG_BIN`, and `FFPROBE_BIN`. It records model and runtime hashes, normalizes audio only in a temporary directory, and deletes it at process exit.
- Stop when a dependency is missing, any output exists, provider evidence is incomplete, or the transcript lacks timestamps. Do not silently use Feishu after a local failure or local Whisper after a Feishu failure.
- For audio, use the verified profile JSON as `source.extraction_profile`. For video, either provider profile deliberately remains incomplete until host vision adds scene-change/key-slide coverage; merge that coverage, remove `video-scene-coverage-required`, and set `complete: true` only after every material scene is represented. Independently hash the final Markdown extraction for `extraction_sha256`.
- Use neutral speaker labels unless identities are provided.
- For video, combine transcript with scene-change/key-slide frames.
- Do not extract frames at a fixed high frequency when scenes do not change.

## Format gates

- DOCX, ODT, RTF: `source_kind: document`; use `native-text`, `vision`, or `hybrid`, with structure/page locators.
- Legacy DOC: require an existing converter and use `converted-text` or `hybrid`; otherwise stop.
- PDF: `source_kind: pdf`; use `native-text`, `ocr`, `vision`, or `hybrid`, with page locators.
- Image: `source_kind: image`; use `ocr`, `vision`, or `hybrid`, with region locators.
- Audio: `source_kind: audio`; use `transcription` or `hybrid`, with timestamp locators.
- Video: `source_kind: video`; use `transcription` or `hybrid`, with timestamp locators and scene-change frames.

Verify that the extension matches the source container. Keep any derived pages, frames, or normalized audio temporary; commit only the untouched original, the canonical Markdown extraction, the knowledge outputs, and normal transaction state.

## Large-source policy

Treat a source as large when any applies:

- more than 100 pages/slides;
- more than 100,000 characters;
- spreadsheet exceeds 20,000 populated cells;
- media exceeds 60 minutes.

For large sources:

1. save the full deterministic extraction locally;
2. build a section/item index;
3. summarize bounded chunks;
4. synthesize a global note from chunk summaries plus representative evidence;
5. verify first, middle, last and anomaly locations;
6. avoid loading the full source into one model context.

Escalate to exhaustive visual inspection only when sampling detects missed layout-dependent information or the source’s meaning is primarily visual.
