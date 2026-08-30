# Layla — Spec

## Repo layout
```
layla/
  agents/
    youtube/
      run.py         # fetch transcript from URL, store in DB
    email/
      run.py          # fetch inbox, propose triage, apply your decisions
  core/
    agent.py          # shared contract: name, description, input_schema, invoke()
    storage.py        # Postgres + pgvector client
    registry.py        # lists agents so Layla can discover them
  db/
    migrations/
  AGENTS.md             # tells any omp session opened here it's Layla, and how
                        # to use the agents (not written yet — see Next steps)
```

## Orchestrator = Layla = this omp session
No separate router process, no dedicated tool-calling LLM code. When you
`omp` into this folder and ask "brief me on important emails" or "grab the
transcript from this video," the session itself:
1. Reads `AGENTS.md` / the agent registry to know what's available.
2. Runs the matching agent script via bash (e.g. `python -m agents.email.run`).
3. Reasons over the raw output itself — summarizes, judges importance — no
   extra LLM API call.
4. Reports back to you and asks for decisions when needed.

## Billing
- Layla's reasoning runs inside your interactive omp session → covered by
  your existing Anthropic account, same as any omp usage. No API key needed
  for this.
- A separate Anthropic API key (pay-per-token) is only required if an agent
  needs to run unattended (e.g. a future cron job with no session present to
  reason for it). Not needed today — both agents are on-demand, invoked by
  you inside a session.

## Agent contract (`core/agent.py`)
Every agent exposes:
- `name: str`
- `description: str` — so Layla/you can pick the right one
- `input_schema` — what args it needs
- `invoke(**kwargs) -> dict` — does the work, returns a structured result

## Storage
- Postgres, single instance, `pgvector` extension enabled.
- Schema: one table per domain — `emails`, `youtube_transcripts` — plus
  embedding columns/tables where semantic search is useful.
- [OPEN] Reuse an existing Postgres instance (e.g. from `remi`/`beef-broth`)
  or provision a fresh one for Layla? Not yet decided.

## Agent: YouTube (on demand)
- Input: video URL.
- Steps: call transcript API → store raw transcript + metadata in
  `youtube_transcripts` → return transcript to Layla.
- [OPEN] Which transcript source (`youtube-transcript-api`, `yt-dlp`, other)?
  Not yet decided.

## Agent: Email (on demand)
- Input: none (scans inbox) or a filter (date range, folder).
- Steps:
  1. Fetch recent emails (Gmail API or IMAP — [OPEN] which, and auth setup).
  2. Return raw list to Layla; Layla summarizes each and proposes
     keep/archive, flagging obvious spam/ads on its own.
  3. Present the proposal to you; you confirm or override per email.
  4. Apply: store "keep" emails (summary + metadata) in the `emails` table;
     archive the rest via the mail provider's API.
- [OPEN] Mail provider/account, auth method (OAuth vs app password).

## Naming
- Folder / orchestrator: **Layla**.
- Per-agent names: not yet decided — referred to by domain (`youtube`,
  `email`) for now.
