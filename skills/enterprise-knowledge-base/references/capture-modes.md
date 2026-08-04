# Capture modes

Run the `collect` access gate before preflight or any local write. Personal content must use `scope: personal`, `publish_to_feishu: false`, and a non-publication state regardless of whether the creator is an administrator or employee. Only reviewed `scope: enterprise` content may later enter a separately confirmed publication batch.

Choose one physical mode before semantic extraction:

- `standalone`: one document or webpage; preserve one original, complete extraction, and source note.
- `standalone-collection`: one large document with stable native items; preserve the complete extraction and a lightweight item index, then create only selective reusable notes.
- `corpus`: documentation site, repository, or multi-file series; preserve hierarchy plus one corpus index.
- `dataset`: workbook or structured dataset; preserve complete data and schema without turning rows into notes.
- `media`: audio or video; preserve the original, timestamped segments, and one topic index.
- `fragment`: explicitly saved chat text, decision, or observation; store only durable units.

For a `standalone-collection`, verify the complete extraction before creating navigation.

- If it has at most 100,000 characters and at most 100 stable items, use `scripts/build_document_index.py`.
- If it exceeds either threshold, use `scripts/split_document_collection.py`.
- Preserve the full canonical extraction under `.kb/evidence/document-collections/<source-sha256>/`.
- Keep the processed-state extraction path as a lightweight landing page.
- Create active volumes of at most 50 native items under `10_来源/提取`.
- Link the item index directly to headings in those volumes.
- Maintain `.kb/state/document_collections.json` with evidence, landing, index, and per-volume hashes.

Never duplicate every native item into `20_知识`.
