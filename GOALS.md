# Layla — Goals

## Vision
Personal assistant. One contact point — Layla, the omp session invoked in this
folder. She handles daily digital chores across domains: YouTube content,
email, calendar, whatever gets added.

## Why
Long-term: an assistant that keeps up with the things you don't have time to
read, with memory of your data over time.

## Architecture
- **Layla is the orchestrator.** No separate router process, no dedicated
  tool-calling service. The interactive session reads domain instructions and
  runs the tools itself.
- **Domains are folders**, not processes. `agents/<domain>/AGENTS.md` holds the
  domain's instructions; `agents/<domain>/*.py` holds its tools. Layla loads
  only the domain in play, so her baseline stays flat as domains grow.
- **Tools are plumbing.** Plain scripts, no LLM calls of their own.
- **Subagents are for verdict work only.** See the mode split below.

## The mode split
The one principle that decides structure per task:

| You want | Shape | Why |
|---|---|---|
| To **discuss** it | material goes into Layla's context | full detail, follow-ups work, she knows your setup |
| A **verdict** | delegate to subagents | bulk text stays out of your conversation |

A digesting middleman either compresses (losing detail you'll ask about later)
or returns everything verbatim (adding nothing). For discussion, both lose. For
verdict work, compression is exactly the point.

Storing the full source alongside the summary resolves the tension: summaries
drive triage, full text is on disk for when you go deep.

## Principles
- Layla's reasoning runs on your existing Claude account login. Subagents too.
  No per-agent API billing.
- On-demand, not scheduled. Unattended runs would need a paid API key for the
  reasoning step; since you only want results when you sit down, session-
  triggered ingest costs nothing and loses nothing.
- Tools hold credentials, Layla doesn't. Narrow provider scopes, secrets from
  keychain/env into the tool process, confirm before mutating.
- Start with the filesystem. Markdown files until a real query need justifies a
  database.

## Current scope
- **YouTube** — ingest a playlist into a local markdown store, triage what's
  worth watching, discuss a video in depth using its timestamped transcript.
  Screenshots pasted by you are read directly; automatic frame extraction is
  the next step after ingest works.

## Later
- **Email** — verdict work: scan inbox, delegate triage, surface only what needs
  your call, archive on your confirmation.
- **Calendar** — not designed yet.
- Automatic frame extraction for slides, charts, diagrams.
- Postgres + pgvector, once cross-video semantic search actually matters.

## Non-goals (for now)
- No standalone orchestrator service.
- No cron/scheduled runs.
- No database — markdown files in `data/`.
- No private-playlist OAuth; unlisted playlists need no auth.
