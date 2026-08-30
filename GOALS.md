# Layla — Goals

## Vision
Personal domain-agent system. Small, focused agents, each owns one chore.
Layla — the omp session invoked in this folder — is the command center: the
reasoning/orchestration layer that talks to you in plain language and decides
which agent to run.

## Why
Long-term: a personal assistant that handles daily digital chores (email
triage, content extraction, whatever gets added later) with memory of your
data over time.

## Principles
- Agents are plumbing: fetch/store data. No built-in LLM calls of their own.
- Layla (the interactive omp session) supplies the intelligence —
  summarizing, judging importance, deciding what to do — using your existing
  Anthropic account. No per-agent API billing.
- One shared Postgres (+ pgvector). One place for all agent data, queryable
  by Layla later.
- Start on-demand for every agent. Add scheduling only when a real need for
  unattended runs shows up.

## Current agents
- **YouTube** — on demand: extract transcript from a video URL, store it.
- **Email** — on demand: scan inbox, summarize, propose keep/archive per
  email (flagging obvious spam/ads on its own), archive what you approve,
  store what you keep.

## Non-goals (for now)
- No standalone orchestrator service or dedicated tool-calling LLM code —
  Layla is the omp session itself.
- No scheduled/cron runs — everything triggered by you, in session.
- No separate vector DB — pgvector inside the same Postgres instance.
