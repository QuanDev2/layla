# Goals

*what this is for · updated 2026-09-20*

---

## 1. Vision

A personal learning system. Feed it an article or a video, talk through it,
keep only what is worth keeping, and find it again months later in your own
words rather than the author's.

The thing it fights is the read-and-forget loop: material consumed once,
half-remembered, never applied. Storage is cheap; curation is not, so curation
is where the human stays in the loop.

## 2. Architecture

- **One agent, no processes.** The interactive session reads `AGENTS.md` and
  calls the Python modules directly. No router, no daemon, no scheduler.
- **Tools are plumbing.** Plain scripts with no LLM calls of their own, except
  `heading_agent.py`, which exists specifically to make one.
- **Everything permanent lives in one SQLite file.** `data/knowledge.db`, with
  the archive (`documents`) kept separate from the retrievable index
  (`snippets`).

## 3. Principles

- **Nothing enters the corpus without an explicit yes.** The agent proposes;
  the human selects. Auto-extraction was considered and rejected — see
  decisions 9 and 10.
- **No failure is fatal.** A dead embedding provider writes the row with a
  NULL vector; a dead heading model falls back to the document's own headings;
  a malformed keyword query degrades instead of raising.
- **Reasoning rides the existing session login.** No per-agent API billing.
  Embeddings run locally on EmbeddingGemma-300M, so there is no paid-tier
  dependency left at all.
- **On-demand, not scheduled.** Results are only read when you sit down, so
  session-triggered work costs nothing and loses nothing.
- **Subject-agnostic.** Tags are free-form and retrieval has no topical
  assumptions. A systems-design paper and a history documentary go through the
  same path.

## 4. Current scope

- **Articles and pasted text** — fetch, capture, discuss, triage, search. Used
  for real.
- **YouTube videos** — captions fetched by `youtube.py`, then treated as a
  document with `source_type='transcript'`. One video at a time.

## 5. Not here, deliberately

- **Playlist triage** — "which of these 30 videos should I watch" was built,
  never used, and deleted in the flatten (decision 21).
- **Bulk import** — a subagent that summarizes many documents at once. Dropped
  (decision 7): the workflow is one document at a time, and bulk mode's
  mechanism is keeping source text out of the conversation, which is exactly
  what discussion needs.
- **A vector database** — brute-force cosine in numpy is ~13ms at 5,000
  snippets. `sqlite-vec` is a scale problem this system does not have.
- **A framework** — LangGraph, ADK, and Mem0 were each evaluated and rejected;
  the evidence is in `research.md`.

## 6. Ideas, not plans

Nothing below is designed. Listing them as "next" would overstate them.

- **Frame extraction** — pull slides and charts from a video and align them
  with the transcript. The pipeline and its database schema are worked out in
  `youtube.md`; no code exists.
- **Email and calendar** — long-standing wishes, blocked on picking a provider
  and an auth model. Neither has a design.
