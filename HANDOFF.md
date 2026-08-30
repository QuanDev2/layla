# Handoff — Layla

## Status
Planning only. No code written yet. Folder renamed from `multi-agent` to
`layla` in this session.

## Decisions made this session
- Structure: monorepo — one repo, shared `core/`, agents as modules
  (originally "Option A" in discussion).
- Orchestrator: no separate service or LLM-router code. The interactive omp
  session invoked in this folder *is* the orchestrator ("Layla"). It reasons
  over agent output directly.
- Billing: confirmed no API key configured locally
  (`~/.omp/agent/config.yml` has empty `providers:`, `CLAUDECODE=1` set) —
  this session already runs on the Claude account login, not pay-per-token
  API billing. Layla rides on that. A separate API key is only needed for
  future unattended/scheduled runs, which are out of scope right now.
- Both agents run on-demand (no cron) for now, including email (originally
  considered periodic, downgraded to on-demand for the first version).
- Storage: single Postgres instance + `pgvector` extension, no separate
  vector DB.
- Email agent workflow: scan → Layla summarizes + proposes keep/archive
  (flagging spam/ads) → you confirm/override per email → "keep" gets stored,
  "archive" gets applied via the mail API.
- Folder/orchestrator name: **Layla**.

## Open questions (not yet decided)
- Postgres instance: reuse one from another repo, or provision a new one?
- YouTube transcript source: which library/API?
- Email provider + auth: Gmail API vs IMAP, OAuth vs app password?
- Per-agent naming beyond "Layla" for the orchestrator?

## Next steps
1. Pick the Postgres instance; add `db/migrations` for `emails` and
   `youtube_transcripts` (+ pgvector).
2. Scaffold `core/agent.py` contract and `core/storage.py`.
3. Build the YouTube agent (`agents/youtube/run.py`) — simplest, on-demand,
   no auth required.
4. Build the email agent (`agents/email/run.py`) — needs the provider/auth
   decision first.
5. Write root `AGENTS.md` so any omp session opened in this folder knows
   it's Layla and how to invoke the agents.
