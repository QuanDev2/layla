# Handoff — Layla

## Status
YouTube ingest pipeline built and verified against real videos. Tools, domain
instructions, and docs are in place. Frame extraction is specified but unbuilt.

## Architecture settled
- **Layla is the orchestrator** — the interactive session, not a service.
- **Domains are folders**, each with its own `AGENTS.md` + tools. Layla loads
  only the domain in play, so her baseline stays flat as domains grow.
- **Mode split decides structure per task:** discussion keeps material in
  Layla's context; verdict work delegates to subagents. Storing the full source
  alongside summaries means delegation loses nothing — summaries drive triage,
  full transcripts stay on disk for deep dives.
- **"YouTube agent" = Layla wearing `agents/youtube/AGENTS.md`**, not a process.

## Decisions made
- No database. Markdown in `agents/youtube/data/videos/` (gitignored). Postgres/pgvector only
  when cross-video semantic search actually matters; markdown stays the source
  of truth so embedding later is a script, not a migration.
- No cron. Unattended runs would need a paid API key for the reasoning step;
  session-triggered ingest rides your existing login and loses nothing since you
  only read results when you sit down.
- Playlists must be public or unlisted — avoids OAuth entirely.
- Credentials: tools hold capabilities, Layla holds none. Secrets go from
  keychain/env into the tool process, never into conversation. Narrow provider
  scopes; confirm before mutating. Not exercised yet — YouTube needs no auth.
- Worth-watching verdicts are computed at triage time, never frozen into
  `summary.md`, because relevance changes week to week.
- Email deferred. It is verdict work and will use subagent delegation.

## Built
| Path | Does |
|---|---|
| `agents/youtube/playlist.py` | List playlist/video metadata via yt-dlp |
| `agents/youtube/run.py` | Timestamped transcript, two sources with fallback |
| `agents/youtube/agent.py` | Summarize one transcript via headless omp subagent |
| `agents/youtube/ingest.py` | One-call ingest pipeline: transcript, summarize, reindex |
| `agents/youtube/index.py` | Regenerate `INDEX.md` from summary frontmatter |
| `agents/youtube/AGENTS.md` | Domain workflows: ingest, triage, deep dive |
| `AGENTS.md` | Layla's identity, domain registry, mode split |

## Verified against real videos
- Playlist listing on a real channel playlist; channel falls back to the
  playlist owner, which flat mode populates.
- Both transcript sources, plus the fallback path with the primary forced to fail.
- Video with no captions fails both sources with a readable error — correct
  behavior, not a bug.
- Two caption artifacts found by testing and fixed: hard line breaks splitting a
  timestamp across lines, and repeated cues (exact duplicates from the API,
  rolling restatements from auto-captions). One dedupe now covers both sources.
- Ingest subagents wrote contract-conforming summaries; index regenerated.

## Next steps
1. Run ingest on your real playlist; confirm the summaries are useful to triage.
2. Lazy frame extraction — `yt-dlp --download-sections` + single-frame ffmpeg
   grab at timestamps the transcript flags. Flag syntax still unverified.
3. Only if context pain shows up: OCR text-only slides instead of shipping
   images (~100 tokens vs ~1.5k).
4. Email domain, once you pick provider and auth.

## Open questions
- Which playlist is the real one to ingest from?
- Batch frame extraction strategy if unattended ingest ever matters — interval
  sampling plus perceptual-hash dedupe is the current plan, untested.
