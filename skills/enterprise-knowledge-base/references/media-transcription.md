# Media transcription routing

Use this route only for audio or video that the user is collecting through the current knowledge project. Resolve the target Feishu space from the project's verified mapping and require an exact match. Never hardcode a space name in a distributable Skill; other visible spaces may belong to unrelated projects or Skill tests.

Run the `media` access gate before preflight. A personal media collection must remain local or use its separately authorized transcription transport only; its knowledge note must keep `scope: personal` and `publish_to_feishu: false`. A later Wiki publication is available only to reviewed enterprise content and requires a fresh `publish` gate plus its own confirmation.

## Choose one provider

- Default: Feishu Minutes under the current user's identity.
- Offline option: an already installed `whisper.cpp` CLI and model.
- Never use Tencent Cloud, a shared credential, a bot identity, or an automatic provider fallback.
- A provider failure is fail-closed. Report the failed stage and preserve any returned Drive or Minute URL so a retry cannot create a duplicate.

## Default Feishu route

Before confirmation, read only this reference. Do not yet load the detailed `lark-shared`, `lark-drive`, or `lark-minutes` instructions; do not authenticate, list remote spaces, inspect remote nodes, upload, or create a Minute.

1. Run the local collection preflight exactly once and lock the source path, name, size, and SHA-256. Immediately start the local timing receipt with the returned hash; it imports that preflight's existing local timing session and makes no remote request:

```powershell
python <skill-root>/scripts/prepare_media_transcript.py receipt-start `
  --vault . `
  --source-sha256 "<preflight-sha256>" `
  --source-name "<source-file-name>"
```

For every later boundary, run `prepare_media_transcript.py receipt-mark --vault . --receipt <receipt> --stage <stage>` immediately after the existing action. Add `--source-file-url` for `drive_uploaded`, `--minute-url` for `minute_created`, and the three committed path arguments for `local_commit_complete`. Do not call a remote API merely to populate timing data.
2. Read `.kb/mappings/feishu_nodes.json` exactly once. Require a non-empty space name and ID plus at least one verified node. Stop on a missing or ambiguous local mapping. Remote revalidation belongs after confirmation and must use this exact mapping; never inspect another visible space merely because it has a similar name.
3. Show one transcription preview containing:
   - the exact local file and hash;
   - upload to the caller's Drive root;
   - creation of one Minute and read-back of its timestamped transcript;
   - reuse of that same Drive original if the reviewed note is published later;
   - no publication of a Wiki document in this step.
   Keep this preview about upload and transcription scope. Do not claim a final personal or enterprise classification from the filename. Mark `preview_ready` after presenting it.
4. Require one explicit confirmation for this upload scope. This confirmation does not authorize later Wiki publication.
5. Mark `confirmed` only after the user confirms. Then read and follow the installed `lark-shared`, `lark-drive`, and `lark-minutes` Skills, including only the Drive upload, Minutes upload, and transcript-detail instructions needed for this operation. Use only `--as user`, request only the smallest missing user scopes, and revalidate the exact mapped space before writing.
6. Upload the source once with `lark-cli drive +upload` from a working directory where the source can be addressed by a relative path. Do not pass a folder or Wiki token. Mark `drive_uploaded` with the returned file URL.
7. Immediately retain the returned file URL in `.kb/temp/media-operation-<source-sha256>.json`. If any later step fails, preserve that receipt, report the URL, and do not upload another copy on retry.
8. Inspect the URL and require a readable object of type `file` with the exact source name. Verify against the mapped enterprise space that it is not a Wiki node.
9. Call `lark-cli minutes +upload --file-token <token>` and mark `minute_created` with its URL. Fetch only the transcript with `minutes +detail --wait-ready --transcript`, then mark `transcript_ready`. Do not request summary, todo, chapter, or keyword artifacts for knowledge ingestion.
10. Create the canonical Markdown transcript and extraction profile:

```powershell
python <skill-root>/scripts/prepare_media_transcript.py feishu-profile `
  --vault . `
  --source "<absolute-media-path>" `
  --transcript ".kb\temp\<minutes-transcript.txt>" `
  --output ".kb\temp\<name>.transcript.md" `
  --profile-output ".kb\temp\<name>.profile.json" `
  --source-file-url "<verified-drive-file-url>" `
  --minute-url "<minute-url>" `
  --minute-token "<minute-token>" `
  --duration-ms <verified-duration-ms>
```

11. Pass the transcript and profile to the internal collection route. In its one semantic pass, classify from transcript or verified visual evidence, never the filename; record the required `classification_review` without asking for another confirmation. Put the exact verified Drive URL in source-note Frontmatter as `source_feishu_file_url`; the collection transaction validates it against the profile.
12. After a successful verified collection commit, mark `local_commit_complete` with the committed `stored_original`, `extraction`, and `source_note` paths, then finalize:

```powershell
python <skill-root>/scripts/prepare_media_transcript.py receipt-finalize `
  --vault . `
  --receipt ".kb\temp\media-operation-<source-sha256>.json"
```

Use the same `mark` command for each named stage; it records only a local timestamp plus the already-returned URL or committed path and never adds a remote request. The finalizer writes `.kb/logs/media-performance/` with preflight, preview, total pre-confirmation, confirmation wait, upload, Minute creation, transcript wait, local collection, and end-to-end durations. It deletes only a successful temporary receipt after matching its complete timing sequence, source hash, Drive URL, Minute URL, stored original, transcript, source note, and managed state. Keep failed or incomplete receipts for duplicate-safe recovery.

For video, the generated profile remains incomplete until host vision adds scene-change or key-slide evidence. Never treat the transcript alone as complete video capture.

## Offline local route

Use this route only when the user explicitly requests local or offline transcription. Do not install or download anything.

Require:

- an existing `whisper-cli` or `WHISPER_CPP_BIN`;
- an existing multilingual model at `WHISPER_CPP_MODEL`;
- existing `ffmpeg` and `ffprobe` commands;
- explicit authorization to execute the local model for this source.

Run:

```powershell
python <skill-root>/scripts/prepare_media_transcript.py local-transcribe `
  --vault . `
  --source "<absolute-media-path>" `
  --output ".kb\temp\<name>.transcript.md" `
  --profile-output ".kb\temp\<name>.profile.json" `
  --allow-local-execution
```

The adapter uses one foreground process, records model and runtime hashes, deletes normalized temporary audio, and never starts a service. If any dependency is missing or the transcript has no timestamps, stop without switching to Feishu automatically.

Local output has no `source_feishu_file_url`; the publication Skill uploads the preserved original only after the later publication confirmation.
