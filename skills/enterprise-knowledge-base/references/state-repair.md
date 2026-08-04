# Collection state repair

Use this reference only when preflight reports a stale state record or a dedicated health check identifies collection-state drift.

Preview:

```powershell
python <skill-root>/scripts/ingest_pipeline.py repair-state `
  --vault .
```

After reviewing the preview, write with backup:

```powershell
python <skill-root>/scripts/ingest_pipeline.py repair-state `
  --vault . `
  --write `
  --repair-missing-text-extractions
```

Recreate only missing TXT or Markdown extractions byte-for-byte. Never synthesize missing PDF, Office, image, audio, or video evidence.
