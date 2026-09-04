---
status: design
updated: 2026-09-03
schema: drafted
scripts: not-started
---

# Knowledge domain — design log

Continuous-learning system: feed in articles, triage them conversationally,
keep what's worth keeping, retrieve it later ("remind me what we learned
about X," "apply the article from last week to what we're building"). This
file is the "what we decided and why," written as the decisions were made —
read it before re-litigating anything below.

## Confirmed decisions (do not re-litigate)

1. **Domain, not a standalone agent.** Lives at `agents/knowledge/`, same
   shape as `agents/youtube/`. Single-article ingest/triage and all
   retrieval happen in Layla's own context (Discussion mode) — both need the
   live conversation ("what are we building") that a separate blank agent
   wouldn't have. See decision 7 for the one place a bootstrapped subagent
   *does* fit.
2. **Storage: SQLite only.** No markdown mirror of curated snippets. Single
   file, agent-mediated, no sync-drift risk. Export to markdown on request
   is a query, not a storage decision, if ever needed.
3. **Retrieval: hybrid.** FTS5 keyword search (built into `sqlite3`, zero
   new dependency) + vector similarity, fused. Add a temporal boost for
   date-language queries ("last week," "recently") — borrowed from Mem0's
   design, directly relevant given the stated use case.
4. **Vector backend: brute-force cosine in numpy, not an ANN index.** At
   personal scale (low thousands of snippets) a full scan is a single
   matrix-vector multiply, sub-millisecond. `sqlite-vec` or similar is a
   scale problem this system doesn't have — revisit only if the corpus
   reaches tens of thousands of snippets.
5. **Embedding provider: Voyage AI for the PoC.** Free tier (200M tokens ×
   several accounts) removes the billing objection for now. Every embedded
   row records `embedding_model` — vectors from different models/providers
   aren't comparable, so switching providers later is an explicit re-embed
   job, never a silent correctness bug.
6. **Local fallback (post-PoC swap target): EmbeddingGemma-300M, Q8_0.**
   Not fp32 — quality delta vs. full precision is 0.23 MTEB points on
   English v2 (68.36 → 68.13), noise-level, at roughly 4x the disk/RAM of
   Q8. Not Q4 either — Q4's extra savings over Q8 aren't worth its slightly
   larger quality gap (0.45 pts) on hardware with no real RAM constraint.
   `fastembed`'s default (`bge-small-en-v1.5`) was rejected in favor of this
   because `bge-small` caps at 512 input tokens, forcing aggressive chunking
   of whole articles; EmbeddingGemma runs 2,048.
7. **Bulk import gets a bootstrapped subagent; single-article triage does
   not.** Mirrors `agents/youtube/agent.py` exactly: a blank `omp -p`
   subprocess per item, `--tools read,write` only (never `task` — must not
   spawn further subagents), `--no-session`, fixed output contract. Used
   only when many articles arrive at once and summarizing article #7 doesn't
   need to know about article #3 or the current conversation. It produces a
   *candidate* summary only — the keep/discard call always happens in
   Layla's own context, never inside the bootstrapped subagent.
8. **No automatic entity extraction (Mem0-style).** Mem0's entity signal
   compensates for two things this corpus doesn't have: atomic
   one-line auto-extracted facts, and pronoun-heavy conversational text.
   Curated multi-sentence excerpts already have low coreference ambiguity,
   and FTS5 (literal terms) + vector search (concepts) + tags (deliberate
   clustering) already cover what entities would add. Additive later if a
   genuine multi-hop/intersection query need shows up — not needed for v1.
9. **Mem0 (the product) is not adopted.** Wrong extraction unit (auto
   LLM-compressed one-liners from chat turns vs. curated article excerpts),
   no triage/human-selection step in its API at all, no document/provenance
   model, and OSS defaults to paid LLM+embedder calls on every add/search.
   Worth reading its source for the retrieval-fusion and temporal-scoring
   ideas; not worth taking as a dependency.
10. **Triage marking is free-form.** You say "keep the part about X"; the
    agent proposes the exact excerpt and shows it back before writing.
    No numbered-paragraph chunking — stays a natural conversation, same
    shape as everything else in this domain's Discussion-mode work.
11. **Tags are free-form,** not a controlled vocabulary. Cheap to retrofit
    a controlled list later if naming drift becomes a real problem;
    premature to lock one in before the corpus exists.
12. **Synthesis snippets are first-class.** Q&A-derived answers from a
    triage session ("how would this apply to X") are savable as
    `kind='synthesis'`, not just source excerpts (`kind='excerpt'`) — often
    more valuable than the raw quote, schema already supports it at no
    extra cost.
13. **`documents` and `snippets` have distinct roles — never redundant.**
    `documents.raw_text` is pure archive/provenance (audit trail, dedup
    check on re-fetching a known URL); it is never searched directly.
    `snippets` is the only retrievable/embedded unit. "Keep the whole
    article" does not mean copying `raw_text` into one giant snippet — an
    entire long article embedded as a single vector produces a mushy,
    undiscriminating representation. It means chunking the whole document
    into several coherent snippets (proposed conversationally, same
    free-form flow as a partial keep), just more of them. Retrieval
    (`search.py`) only ever queries `snippets`; `documents` is joined back
    afterward solely to attach source `title`/`url`/`ingested_at` to a hit.
14. **Chunking is structural, target ~1,800 chars, hard cap 7,000 chars —
    characters, not tokens.** Boundaries follow the document's own
    structure (headings, paragraph groups, complete code examples) — never
    split mid-code-block or mid-sentence. The cap exists because it is
    EmbeddingGemma's input limit (2,048 tokens); 7,000 chars ≈ 2,000 tokens
    at a deliberately conservative 3.5 chars/token estimate, so it
    over-counts and never overshoots the real limit — no tokenizer, no new
    dependency. No minimum size — a one-paragraph snippet is fine if it's a
    complete, useful excerpt. Code fences are atomic: a fenced block is
    never split, and a single unit over the cap (one huge fence or one
    enormous sentence) is never truncated either — it is stored whole as one
    chunk flagged `over_cap`, so FTS5 indexes all of it even though the
    embedding only covers what fits the model's window. Layla proposes the
    boundaries and shows them before writing, same free-form/confirm flow as
    decision 10 — chunking is never silent, even for a "keep whole" article.
    Full build spec: "Chunking implementation plan" below.
15. **Zero stored overlap; read-time neighbor expansion.** For whoever
    builds `search.py`: a hit on a snippet with a non-NULL `chunk_index` is
    joined with `chunk_index - 1` and `+ 1` from the same `document_id`
    where those rows exist and have `kind='excerpt'`. A gap — from a chunk
    the user discarded — simply means no expansion on that side, never a
    re-read of `raw_text`. This is why discarded material can never leak
    back in through expansion. Rationale: duplicated text in FTS5 returns
    the same passage twice in top-k; a read-time join keeps one copy, sharp
    scores, and a window retunable without re-embedding.
16. **Structure normalization precedes splitting**, via a cheap LLM emitting
    insertion points only (never document text), addressed by unit index
    rather than source line, anchor-verified with ±5-unit repair, one
    scoped retry, then deterministic fallback. This removes any
    structured-vs-unstructured routing decision from the splitter — one
    splitter handles every document, article or transcript.
17. **Backend pluggability.** `KNOWLEDGE_LLM` = `omp` (default) | `anthropic`
    | `off`; the deterministic splitter runs whenever no backend is usable,
    so the domain works off an omp harness with degraded boundaries rather
    than not at all.
18. **Context prefix prepended into `content`** using the per-source
    templates in the build spec below, so a chunk carries its subject into
    both the vector and the FTS index.
19. **Rejections are storable knowledge.** When the user rejects part of a
    document, that text is never stored as an `excerpt`, but Layla offers
    to store the *judgment* as `kind='rejection'` holding the reason. A
    distinct kind rather than prose inside a `synthesis` snippet, because
    polarity must be structural — a retrieval hit must never read back as
    an endorsement — and `search.py` can then surface it when a later
    document repeats the rejected claim. Offered and confirmed, never
    automatic, consistent with decisions 10 and 14.

## Schema (drafted, not yet built)

```sql
CREATE TABLE documents (
  id              INTEGER PRIMARY KEY,
  url             TEXT,                    -- nullable: pasted text has no URL
  title           TEXT,
  source_type     TEXT NOT NULL,           -- 'url' | 'pasted' | 'file' | 'transcript'
  ingested_at     TEXT NOT NULL,           -- ISO 8601
  raw_text        TEXT NOT NULL,
  structured_text TEXT,                    -- heading-annotated derived copy; raw_text stays pristine
  agent_summary   TEXT,
  status          TEXT NOT NULL DEFAULT 'pending'  -- 'pending' | 'kept' | 'partial' | 'discarded'
);

CREATE TABLE snippets (
  id              INTEGER PRIMARY KEY,
  document_id     INTEGER NOT NULL REFERENCES documents(id),
  content         TEXT NOT NULL,
  kind            TEXT NOT NULL DEFAULT 'excerpt',  -- 'excerpt' | 'synthesis' | 'rejection'
  chunk_index     INTEGER,                           -- position in document order; NULL for synthesis/rejection
  tags            TEXT,                              -- comma-separated, free-form
  created_at      TEXT NOT NULL,
  embedding       BLOB,                               -- raw float32 vector bytes
  embedding_model TEXT NOT NULL                        -- e.g. 'voyage-3', 'embeddinggemma-q8'
);

CREATE VIRTUAL TABLE snippets_fts USING fts5(
  content, tags, content='snippets', content_rowid='id'
);
```

`documents.raw_text` is kept even for discarded documents — provenance
survives triage, and nothing is lost if a session is abandoned before
triage completes (`status` starts at `pending` the moment content is
fetched).

## Flow

```mermaid
flowchart TD
    A[You feed a URL/article] --> B[documents row written, status=pending]
    B --> C[Agent summarizes]
    C --> D[Q&A in conversation]
    D --> E{Triage decision}
    E -->|keep whole/parts| F[Write snippet: excerpt or synthesis]
    E -->|discard| G[documents.status=discarded, no snippets]
    F --> H[Embed: Voyage now, EmbeddingGemma Q8_0 later]
    H --> I[snippets_fts indexed]
    I --> J[documents.status=kept/partial]

    K["remind me about X" / "apply article from last week"] --> L[FTS5 + vector search, fused, temporal boost]
    L --> M[Ranked snippets + source title/date]
    M --> N[Agent synthesizes answer, cites source]
```

## Repo layout (planned, not yet built)

```
agents/knowledge/
  AGENTS.md         — domain instructions Layla reads for knowledge-touching requests
  db.py             — schema + connection helper
  ingest.py         — capture: fetch content, create documents row
  chunk.py          — unit segmentation, structural splitter, context prefixes
  heading_agent.py  — heading-insertion call; pluggable backend (omp | anthropic | off)
  triage.py         — write snippets from triage decisions
  embed.py          — provider abstraction (Voyage now, swappable to local)
  search.py         — hybrid search: FTS + vector + fusion
  summarizer_agent.py  — bulk-import only; bootstrapped subagent, mirrors
                          agents/youtube/agent.py
  data/             — gitignored: knowledge.db; also holds verification fixtures (LESSONS-ai-native-sdlc-playbook.md, ai-native-sdlc-playbook.html)
```

## Considered and deferred

- **Consolidation gate** (periodic summarizer pass that distills
  overlapping/redundant snippets into one durable note, pattern seen in a
  reviewed video on general AI agent memory design). Legitimate idea, but
  the video's version runs automatically after a message-count threshold —
  that's the same silent-mutation shape already rejected when Mem0's
  ADD-only auto-extraction was ruled out. If ever added, it must be
  human-gated like everything else here: propose the merge, show the
  result, get approval, never silent. Not needed yet — this corpus is
  curated on the way in (nothing enters `snippets` without an explicit
  keep decision), so it grows into a redundancy problem far slower than a
  system that logs everything by default.

## Future migration tasks

- **Re-embed job: Voyage → EmbeddingGemma-300M Q8_0.** Triggered whenever
  the switch to the local model actually happens (e.g. Voyage's free tier
  runs out). Not needed for the PoC — noted here so it isn't rediscovered
  as a surprise later.
  - **What it does:** for every row where `embedding_model != 'embeddinggemma-q8'`,
    re-run the already-stored `content` (untouched by this job) through
    EmbeddingGemma and overwrite `embedding` + `embedding_model` on that row.
  - **Why it's safe:** `content` is the source of truth; `embedding` is a
    derived, regenerable index. Nothing about switching providers touches
    the actual text, so no data is at risk — only the vectors go stale
    until this job runs.
  - **Why it's cheap:** the destination model is local and free — this is
    CPU time on-device, not a paid API bill, unlike a hypothetical reverse
    migration.
  - **What still works before the migration runs:** FTS5 keyword search
    over `content` is completely unaffected by which embedding model is
    active — old rows stay fully keyword-searchable the entire time. Only
    vector/semantic search on unmigrated rows is degraded in the gap
    between switching the active provider and running this job.
  - **Guard while both embedding spaces briefly coexist:** `search.py`
    should only run cosine similarity against rows whose `embedding_model`
    matches the currently active provider — comparing vectors across
    incompatible spaces produces silently wrong rankings, not an error, so
    this must be an explicit filter, not an oversight.

## Chunking implementation plan

Build spec for decisions 14-19 above: `agents/knowledge/chunk.py` (unit
segmentation and structural splitting, character-based sizing) plus
`agents/knowledge/heading_agent.py` (heading-insertion call with a
pluggable backend), verifiable standalone against two real fixtures in the
repo, with no database. Heterogeneous input makes this load-bearing:
articles carry markdown headings, YouTube transcripts carry none.

Scope boundary: `db.py`, `search.py`, and read-time neighbor expansion are
not built here (those files do not exist); expansion is *specified* in
decision 15 above for whoever builds `search.py`.

### Approach

#### Step 1 — Package init

Create empty `agents/knowledge/__init__.py` (0 bytes), matching
`agents/__init__.py` and `agents/youtube/__init__.py`, so
`python3 -m agents.knowledge.chunk` resolves.

**Verification**

Manual: `python3 -c "import agents.knowledge; print('ok')"` from the repo
root — prints `ok`, no `ImportError`. Nothing else to check; an empty
`__init__.py` has no behavior beyond importability.

#### Step 2 — `agents/knowledge/chunk.py`: unit segmentation

Boundaries are never derived from the file's own line breaks. A transcript may
arrive as one unbroken line, and prose is often hard-wrapped at a column width
that has nothing to do with its structure. So `chunk.py` first segments the
document into **atomic units it constructs itself**, and every downstream position
— model-facing and stored — refers to those units, never to source lines.

```python
NAME = "knowledge_chunker"
DESCRIPTION = "Split a document into retrieval-sized candidate snippets with context prefixes."

TARGET_CHARS = 1800
MAX_CHARS = 7000
ANCHOR_WINDOW = 5
```

`segment_units(text: str, source_kind: str) -> list[dict]`

Each unit is `{"index": int, "text": str, "start": int, "end": int, "stamp": str | None}`
where `start`/`end` are character offsets into `text` such that
`text[start:end] == unit["text"]`. `index` is 0-based and contiguous. The character
offsets are the real position record; the index is only the handle the model is
given. Offsets make a boundary exact and independent of line breaks, and they are
what `split_body` slices with.

Unit granularity by shape, chosen in this order:

1. **Transcript** (`source_kind == "transcript"`): one unit per `[MM:SS]` segment,
   `stamp` set. Guaranteed one segment per line by
   `agents/youtube/AGENTS.md:118-119` (caption line breaks normalized away, rolling
   captions collapsed by `run.py`), so this parse is safe for `transcript.md`.
2. **Article with blank lines**: one unit per blank-line-delimited paragraph.
3. **Article with no blank line, or any paragraph over `TARGET_CHARS`**: split into
   sentences — `[.?!]` followed by whitespace or end of string. This is the branch
   that handles a single-long-line document; the absence of newlines is irrelevant
   because units come from sentence punctuation, not layout.

A markdown heading line (`^#{1,6} `) is always its own unit with its `#` markers
intact, so a document that already has structure keeps it through segmentation.
A fenced code block (` ``` ` to its closing fence) is always exactly one unit,
however long — this is what makes fences unsplittable later.

If sentence splitting still yields a unit over `MAX_CHARS` (one enormous sentence,
or a single fenced block), keep it as one unit and let step 5 flag it `over_cap`.
Never split a unit at an arbitrary character position.

**Transcript parsing** (`parse_transcript(text) -> tuple[dict, list[dict]]`,
returning frontmatter plus units):

- Skip a leading `---` YAML block, parsed with the same simple loop as
  `agents/youtube/index.py:parse_frontmatter` (`key: value` per line).
- Each segment matches `^\[(\d{1,2}:\d{2}(?::\d{2})?)\]\s*(.*)$`. Scan with
  `re.finditer` over the whole body rather than line-by-line, so a transcript
  collapsed onto one line — several `[MM:SS]` markers in a single line — still
  segments correctly. Each match starts a unit and runs to the next match or end.
- `_parse_stamp(s) -> int` converts `MM:SS` and `H:MM:SS` to seconds by splitting on
  `:` and folding right-to-left. Local helper: `run.py`'s `_parse_vtt_timestamp`
  parses WebVTT `HH:MM:SS.mmm`, a different format.
- Text before the first `[MM:SS]` match is dropped; a body with zero matches falls
  back to the article path (rule 2/3 above) with every `stamp` left `None`, so a
  mislabeled or caption-free file still chunks instead of crashing.
- Each chunk's `start`/`end` timestamps are the `stamp` of its first and last unit
  carrying one.

**Verification**

Manual — run `segment_units` against both fixtures and read the unit
boundaries directly, no assertions:

```
python3 -c "
from agents.knowledge.chunk import segment_units
text = open('agents/knowledge/data/LESSONS-ai-native-sdlc-playbook.md').read()
units = segment_units(text, 'article')
print(len(units), 'units')
for u in units[:8]:
    print(u['index'], u.get('stamp'), repr(u['text'][:70]))
"
```

then the same against
`agents/youtube/data/videos/ow1we5PzK-o/transcript.md` (body only, after
the frontmatter) with `source_kind='transcript'`.

Read for: article units break at paragraph/heading boundaries with no unit
spanning a blank line; any `#`-heading line is isolated as its own unit; a
fenced code block (if the Lessons fixture has one) prints as a single unit
even though it contains embedded newlines; transcript units number ~534,
`stamp` is set on every one, and each unit's text starts right after its
`[MM:SS]` marker with no marker leaking into two units.

#### Step 3 — `agents/knowledge/heading_agent.py`: heading-insertion call

Named for the sibling convention (`agents/youtube/agent.py`, and
`summarizer_agent.py` in this domain's planned layout) — a module that wraps a
headless `omp -p` subprocess. Module names cannot contain hyphens and stay
importable, so the underscore form is the available one.

Conventions mirror `agents/youtube/agent.py`: module docstring in the repo's
`In:`/`Out:`/`State:` style, `NAME`, `DESCRIPTION`, `INPUT_SCHEMA`, `invoke()`
returning a dict that never raises for expected failures, `main()` printing JSON
and returning 0/1.

```python
NAME = "knowledge_heading_inserter"
DESCRIPTION = "Propose heading insertion points for a document that lacks usable structure."

DEFAULT_MODEL = "anthropic/claude-haiku-5"
DEFAULT_EFFORT = "low"
TOOL_NAMES = ["read", "write"]
```

`insert_headings(units: list[dict], source_kind: str, model=DEFAULT_MODEL, effort=DEFAULT_EFFORT, feedback=None) -> dict`

- Takes the units from step 2, not raw text — the caller owns segmentation.
- `source_kind` is `"article"` or `"transcript"`; it selects prompt wording only.
- Returns `{"ok": True, "points": [{"before_unit": int, "anchor": str, "heading": str}], "backend": str}`
  or `{"ok": False, "error": str, "backend": str}`. `points` MAY be empty — that is
  the "already well structured, changed nothing" result, not a failure.
- Backend from `os.environ.get("KNOWLEDGE_LLM", "omp")`. Value `off` returns
  `{"ok": False, "error": "KNOWLEDGE_LLM=off", "backend": "off"}` without a call.
  An unrecognized value returns `ok: False` with `error` naming the bad value.

**Numbered input.** `render_units(units) -> str` builds one block per unit as
`f"[{u['index']}] {u['text']}"`, joined by blank lines. A unit's own internal
newlines are preserved, so a code block still reads as a code block. Indices are the
only positional vocabulary the model is given — source line numbers never appear in
its input, so it cannot reference them.

**`omp` backend.** Write the rendered units to a temp file and the model's JSON to a
second temp path, then read that file — do not parse stdout. This copies
`agents/youtube/agent.py:133-158` exactly, including its
`if not Path(out).exists()` failure check, because file-output is already this
repo's proven contract and stdout parsing is fragile.

```python
cmd = [
    "omp", "-p",
    "--tools", ",".join(TOOL_NAMES),
    "--no-extensions", "--no-skills", "--no-rules",
    "--approval-mode", "yolo",
    "--model", model,
    "--thinking", effort,
    "--no-session",
    "--system-prompt", prompt,
    f"Read {units_path}. Write the JSON array to {out_path}.",
]
subprocess.run(cmd, capture_output=True, text=True, timeout=180)
```

Handle `FileNotFoundError` → `{"ok": False, "error": "omp not found on PATH"}` and
`subprocess.TimeoutExpired` → `{"ok": False, "error": "omp headless call timed out after 180s"}`,
matching the sibling's two guards. Use `tempfile.TemporaryDirectory()` for both paths.

**`anthropic` backend.** Import `anthropic` lazily inside the branch so the package
stays optional and absent-import returns
`{"ok": False, "error": "anthropic package not installed"}`. Call
`client.messages.create(model="claude-haiku-4-5", max_tokens=2000, system=prompt, messages=[{"role": "user", "content": rendered_units}])`,
then `json.loads` the first content block's text. Missing `ANTHROPIC_API_KEY` →
`{"ok": False, "error": "ANTHROPIC_API_KEY not set"}`.

**System prompt** (`build_system_prompt(source_kind, feedback=None)`, pure, mirroring
`agent.py:100-118` including its `"# Previous attempt failed\n" + feedback +
"\nFix exactly these issues. Change nothing else."` retry block):

```
You mark section boundaries in a document. You never rewrite, summarize, or
reproduce its text.

The document is given as numbered blocks, each starting with "[N]". A block is one
paragraph, one sentence, one transcript segment, or one code block.

Write a JSON array. Each element marks one heading to insert:
  {"before_unit": N, "anchor": "first six words of block N", "heading": "Section title"}

Rules:
- before_unit is the number of the block the heading goes immediately BEFORE.
- anchor must copy the first six words of block N verbatim, so placement is verifiable.
- heading is your own short title (under 80 characters), not a quote from the text.
- Insert a heading only where the topic genuinely changes. Aim for sections of
  roughly 1800 characters; never leave a section longer than 7000 characters.
- If the document already has clear headings covering its topics, write [] and stop.
- Write only the JSON array. No prose, no code fence, no document text.
```

For `source_kind == "transcript"`, append: `"Blocks are transcript segments prefixed with [MM:SS]. Topic changes are where the speaker moves to a new subject."`

**Verification**

Manual, three runs — this step wraps a live LLM call, so "success" is
read-and-judge, not asserted:

```
python3 -c "
from agents.knowledge.chunk import segment_units
from agents.knowledge.heading_agent import insert_headings
text = open('agents/youtube/data/videos/ow1we5PzK-o/transcript.md').read().split('---', 2)[2]
units = segment_units(text, 'transcript')
r = insert_headings(units, 'transcript')
print(r['backend'], len(r.get('points', [])))
for p in r.get('points', []):
    print(p['before_unit'], p['heading'], '|', units[p['before_unit']]['text'][:70])
"
```

1. Against the transcript fixture: `backend == 'omp'`, points list
   non-empty, and for each printed point the unit text actually matches
   the anchor and heading topic — read a handful, not all.
2. Against `agents/knowledge/data/LESSONS-ai-native-sdlc-playbook.md` (already has 18 real
   headings): expect `points == []` — the model recognizing existing
   structure and declining to add more is the pass condition, not an
   empty result to be suspicious of.
3. `KNOWLEDGE_LLM=off` re-run of either: `r['ok'] is False`,
   `r['backend'] == 'off'`.

#### Step 4 — Validation and repair

In `chunk.py`: `validate_points(points, units) -> tuple[list, str | None]`

Applied in order; returns repaired points plus a scoped failure message (`None` when
clean). The message is fed straight back into one retry, so it must name the exact
offenders like `agents/youtube/PLAN.md:109-110` requires — never a blanket string.

1. Drop non-dict elements and elements missing `before_unit` or `heading`.
2. Coerce `before_unit` to `int`; reject out of `0..len(units)`.
3. Anchor repair: compare `anchor`'s first six whitespace-separated words,
   casefolded and stripped of a leading `[MM:SS]` marker, against
   `units[before_unit]["text"]`. On mismatch, scan `before_unit ± ANCHOR_WINDOW`
   for a unit whose first six words match and move the point there. No match in the
   window → drop the point and record it. Unit indices drift far less than line
   numbers did, since a unit is a whole paragraph or segment, but the echo stays as
   the correctness check.
4. Reject empty headings and headings over 80 chars; strip any leading `#` the model
   added, since `chunk.py` writes the `##` marker itself.
5. Sort by `before_unit`; drop duplicates at the same index, keeping the first.
6. Drop any point landing immediately before a unit that is already a markdown
   heading — the document had structure there and a second heading would create an
   empty section.
7. Sum unit lengths between consecutive points. Any resulting section over
   `MAX_CHARS` → failure message naming it, e.g.
   `"section starting at block 210 is 9,240 chars, over the 7,000 cap — insert a heading inside it"`.

Retry policy: one retry via `heading_agent.insert_headings(..., feedback=message)`.
If the second attempt still fails validation, discard the LLM result entirely and
split on whatever headings the document already had, reporting
`backend: "fallback"`.

Applying accepted points produces `structured_text`: units re-joined in order with
`f"## {heading}"` inserted before the unit named by each point. Because units are
exact slices of the source, re-joining cannot alter, drop, or duplicate text —
Verification check 4 asserts this byte-for-byte.

**Verification**

Manual — hand-build a bad `points` list against a real fixture's units and
read what comes back, no assertions:

```
python3 -c "
from agents.knowledge.chunk import segment_units, validate_points
units = segment_units(open('agents/knowledge/data/LESSONS-ai-native-sdlc-playbook.md').read(), 'article')
def first6(i): return ' '.join(units[i]['text'].split()[:6])
bad = [
    {'before_unit': 9999, 'anchor': 'nonsense', 'heading': 'Out of range'},
    {'before_unit': 5, 'anchor': first6(8), 'heading': 'Drifted anchor'},
    {'before_unit': 3, 'anchor': first6(3), 'heading': 'Dup A'},
    {'before_unit': 3, 'anchor': first6(3), 'heading': 'Dup B'},
]
points, msg = validate_points(bad, units)
for p in points: print(p)
print('message:', msg)
"
```

Read for: the `before_unit: 9999` point is gone; the drifted one now reads
`before_unit: 8` (moved to match its anchor, not left at 5); only one of
the two `before_unit: 3` duplicates remains.

Separately, force the two failure paths that produce a retry message, not
just a silent repair: an anchor with no match anywhere in `±5` units, and a
hand-built `points` list whose gaps make one section exceed `MAX_CHARS`.
Read that `msg` names the specific unit/section, e.g. contains `"section
starting at block"` or the dropped anchor's text — a generic string here is
a bug per decision 16 / `agents/youtube/PLAN.md:109-110`.

#### Step 5 — Structural split

`split_sections(units, points) -> list[dict]` cuts at every heading — both the
document's own heading units and the inserted points — producing
`{heading_path, units}` where `heading_path` is the list of enclosing headings from
the document's hierarchy (an `###` under an `##` inherits both; an inserted heading
is always `##`).

`split_body(section_units) -> list[list[dict]]` handles a section over
`TARGET_CHARS` by accumulating units until adding the next would exceed it, then
cutting. Units are already the finest legal boundary from step 2, so no further
splitting rule is needed and no cut can land mid-sentence or mid-segment. A section
at or under `TARGET_CHARS` yields exactly one chunk and is never merged with a
neighbor — decision 14 sets no minimum size, and merging would blur two topics.

A chunk's body is `text[first_unit["start"]:last_unit["end"]]`, sliced from the
source by character offset, so whitespace between units is preserved exactly as
written.

**Code fences are atomic** because step 2 makes a fenced block one unit — there is no
parity tracking to get wrong here. A single unit over `MAX_CHARS` (a huge fenced
block or one enormous sentence) becomes one chunk with `over_cap: True`. It is
stored whole so FTS5 indexes all of it, and the embedding covers only what fits the
model's window. Truncating would lose code and splitting a code example arbitrarily
is worse than one blunt vector; the flag makes it a known limitation rather than a
silent one, and `format_candidates` surfaces it.

**Verification**

Manual — run the full chunker on both fixtures and read the per-chunk
table:

```
python3 -c "
from agents.knowledge import chunk
r = chunk.invoke('agents/knowledge/data/LESSONS-ai-native-sdlc-playbook.md', kind='article', title='Lessons')
for c in r['chunks']:
    print(c['chunk_index'], c['chars'], c['over_cap'], c['prefix'])
"
```

Read for: `chars` clusters near 1,800 with none over 7,000 unless
`over_cap` is `True`; chunk count and prefixes line up with the fixture's
real heading structure in document order (none skipped, none repeated);
add `c['content'][:80]` and confirm no chunk body starts or ends
mid-sentence. If the fixture has a fenced code block, confirm it sits
wholly inside exactly one chunk, never split across two.

#### Step 6 — Context prefix

`build_prefix(source_kind, doc_title, heading_path, start=None, end=None, channel=None) -> str`

|`source_kind`|Prefix|
|---|---|
|`article`|`Title > Section > Subsection`|
|`transcript`|`Video Title — Channel [12:34–15:02] > Section`|

Rules: join `heading_path` with ` > `; omit any empty component and its separator, so
an untitled document or an unnamed channel degrades cleanly instead of emitting
`" > "`. Stored `content` is `f"{prefix}\n\n{body}"`. `chars` and the `MAX_CHARS`
check both count the full `content` including the prefix, since that is what gets
embedded.

`video_title` and `channel` are optional parameters, not read from disk —
`transcript.md`'s frontmatter carries `video_id`/`url`/`language` only, while
`title`/`channel` live in the sibling `summary.md` (`agents/youtube/index.py:_FIELDS`).
Whichever caller has that metadata passes it in.

**Verification**

Manual — call it directly with the cases the template table has to cover,
and read each string:

```
python3 -c "
from agents.knowledge.chunk import build_prefix
print(repr(build_prefix('article', 'Doc Title', ['Intro', 'Sub'])))
print(repr(build_prefix('article', '', ['Intro'])))
print(repr(build_prefix('transcript', 'Video', ['Section'], start='12:34', end='15:02', channel='Chan')))
print(repr(build_prefix('transcript', 'Video', [], start='0:00', end='2:00', channel='')))
"
```

Read for: the full article case reads `Doc Title > Intro > Sub`; missing
title degrades to `Intro`, not `' > Intro'`; the full transcript case reads
`Video — Chan [12:34–15:02] > Section`; missing channel and empty heading
path degrades to `Video [0:00–2:00]` with no stray ` — ` or ` > `.

#### Step 7 — Public API and candidate table

```python
def chunk_article(text: str, doc_title: str = "", *, use_llm: bool = True) -> dict
def chunk_transcript(text: str, *, video_title: str = "", channel: str = "", use_llm: bool = True) -> dict
def format_candidates(result: dict) -> str
def invoke(path: str, kind: str = "article", title: str = "", channel: str = "", use_llm: bool = True, **_kwargs) -> dict
def main() -> int
```

Both chunkers return:

```python
{"ok": True,
 "source_kind": "article" | "transcript",
 "backend": "omp" | "anthropic" | "fallback" | "passthrough",
 "headings_inserted": int,
 "structured_text": str,
 "chunks": [{"chunk_index": int,      # 0-based, position in document order
             "prefix": str,
             "content": str,          # prefix + "\n\n" + body, exactly what gets stored
             "chars": int,
             "over_cap": bool,
             "start": str | None,     # transcript only
             "end": str | None}]}
```

`backend: "passthrough"` means the LLM ran and returned `[]` — already well
structured. `"fallback"` means no LLM result was usable.

`format_candidates` renders the table Layla shows before any write: index, chars, an
`!` marker when `over_cap`, the prefix, and the body's first ~60 chars. This is the
confirm step — `chunk.py` never writes to a database and has no write path at all.

CLI matches every sibling module (`agents/youtube/AGENTS.md:16`: prints JSON to
stdout, exit 0 on success, 1 on failure), with `--kind`, `--title`, `--channel`,
`--no-llm`, and `--table` to print `format_candidates` instead of JSON.

Empty or whitespace-only input returns
`{"ok": False, "error": "empty document"}`; a missing path returns
`{"ok": False, "error": "file not found: <path>"}`. Neither raises.

**Verification**

Manual — this is the full pipeline through the public API and CLI, what
everything above ships to. The steps above verify their own internals;
this verifies the assembled result. Run from the repo root. Fixtures
confirmed present: the transcript is 544 lines / 22,106 chars with 534
`[MM:SS]` segments (auto-generated captions — the hardest boundary case),
and `agents/knowledge/data/LESSONS-ai-native-sdlc-playbook.md` is 236 lines / 13,243 chars with
18 heading lines. `omp` must be on PATH for checks 1 and 3.

1. **Already-structured article passes through.**
   `python3 -m agents.knowledge.chunk agents/knowledge/data/LESSONS-ai-native-sdlc-playbook.md --kind article --title "LESSONS: AI-native SDLC playbook" --table`
   Expect `backend: "passthrough"` with `headings_inserted: 0` (18 real
   headings already cover its topics), roughly 8-14 chunks, every `chars`
   ≤ 7,000, and each prefix reading `LESSONS: AI-native SDLC playbook >
   <one of its real headings>`.

2. **Deterministic path produces the same split.**
   `KNOWLEDGE_LLM=off python3 -m agents.knowledge.chunk agents/knowledge/data/LESSONS-ai-native-sdlc-playbook.md --kind article --title "LESSONS: AI-native SDLC playbook"`
   Expect `backend: "fallback"` and an identical chunk count and identical
   `content` values to check 1 — the article's own headings drive the
   split, so the LLM step changing nothing must be observable, not
   assumed.

3. **Structureless transcript gains headings and timestamps.**
   `python3 -m agents.knowledge.chunk agents/youtube/data/videos/ow1we5PzK-o/transcript.md --kind transcript --table`
   Expect `headings_inserted` ≥ 5, roughly 12-16 chunks from 22,106 chars,
   every chunk carrying `start`/`end` `[MM:SS]` strings with `start`
   strictly increasing across `chunk_index`, and each prefix ending in an
   inserted section title. No chunk body may begin or end
   mid-`[MM:SS]`-line — grep each `content` for `\[\d+:\d\d\]` and confirm
   every match sits at a line start.

4. **No text lost, added, or duplicated** (proves both the
   anti-hallucination guarantee of step 2 and zero overlap):
   ```
   python3 -c "
   from agents.knowledge import chunk
   r = chunk.invoke('agents/youtube/data/videos/ow1we5PzK-o/transcript.md', kind='transcript')
   bodies = ''.join(c['content'].split(chr(10)+chr(10), 1)[1] for c in r['chunks'])
   import re
   norm = lambda s: re.sub(r'\s+', ' ', re.sub(r'(?m)^#{1,6} .*$', '', s)).strip()
   src = norm(open('agents/youtube/data/videos/ow1we5PzK-o/transcript.md').read().split('---',2)[2])
   print('MATCH' if norm(bodies) == src else 'MISMATCH')
   "
   ```
   Must print `MATCH`. A mismatch means the splitter dropped or duplicated
   source text, or the model's output leaked into content.

5. **Transcript survives with no backend.**
   `KNOWLEDGE_LLM=off python3 -m agents.knowledge.chunk agents/youtube/data/videos/ow1we5PzK-o/transcript.md --kind transcript --table`
   Expect `backend: "fallback"`, still-valid chunks under the cap with
   correct timestamp ranges, and prefixes carrying the timestamp range
   with no section title. Check 4's identity must still print `MATCH`
   under this backend.

6. **Bad model output is caught, not trusted.** Call
   `chunk.validate_points` directly against the units of any fixture, with
   a hand-built list containing an out-of-range `before_unit`, a point
   whose `anchor` matches the unit three positions below its stated
   `before_unit`, and a duplicate index. Expect the out-of-range point
   dropped, the drifted point silently moved to the matching unit, the
   duplicate collapsed, and a returned message naming the dropped point
   specifically rather than a generic failure string.

### Critical files & anchors

|Path|Anchor|Why|
|---|---|---|
|`agents/youtube/agent.py`|`invoke()` at 121-158, `build_system_prompt()` at 100-118|The exact `omp -p` flag set, timeout/guard pattern, file-output contract, and retry-feedback wording to copy in `heading_agent.py`.|
|This file|Decisions 1-13 vs. 14-19 above|Decisions 1-13 predate this build spec; 14-19 settle and extend decision 14 for it. None of decisions 1-19 are open for re-litigation.|
|`agents/youtube/AGENTS.md`|Lines 116-122|Transcript format guarantees: one `[MM:SS]` segment per line, rolling captions already collapsed. The transcript parser depends on both.|
|`agents/youtube/index.py`|`parse_frontmatter()` at 32-38, `_FIELDS` at 29|Frontmatter loop to mirror, and confirmation that `title`/`channel` live in `summary.md`, not `transcript.md`.|
|`agents/youtube/PLAN.md`|Lines 94-113|Established scoped-feedback retry contract that `validate_points` messages must satisfy.|

### Assumptions & contingencies

- **Haiku-tier model string is `anthropic/claude-haiku-5`** for the `omp` backend
  (`agents/youtube/agent.py:30` uses `anthropic/claude-sonnet-5`, so the tier naming
  is inferred, not verified). If `omp` rejects it, set
  `DEFAULT_MODEL = "anthropic/claude-sonnet-5"` and keep `DEFAULT_EFFORT = "low"`;
  the call is one small JSON emission, so effort matters more than tier.
- **The `anthropic` SDK model id is `claude-haiku-4-5`.** If the SDK rejects it, list
  available ids and take the cheapest haiku-tier one. Do not add `anthropic` to
  `requirements.txt` — the import is lazy and the backend is opt-in, so the repo's
  single-dependency, API-key-free default is preserved.
- **`omp` accepts `--tools read,write` with a temp-file output contract**, verified in
  use at `agents/youtube/agent.py:135`. If the heading model proves able to write
  valid JSON only to stdout, parse stdout by scanning from the first `[` to its
  matching `]` and keep the one-retry-then-fallback policy unchanged.
- **Target ~1,800 chars** is tuned for retrieval sharpness over context richness. If
  check 3 yields chunks that read as fragments mid-argument, raise `TARGET_CHARS` to
  2,600 (~650 tokens); do not add stored overlap, since read-time neighbor expansion
  (decision 15) is the mechanism for that problem.
- **Voyage's behavior on over-limit input is unconfirmed** — it may error or silently
  truncate. This only affects `over_cap` chunks, which check 1 and 3 should report as
  zero on both fixtures. Whoever builds `embed.py` must confirm it and, if Voyage
  errors, embed a `MAX_CHARS`-truncated copy while storing `content` whole.

## Not started

Every file under "Repo layout" above — no code has been written yet. The
domain's architecture (decisions 1-13) and the chunking unit's full build
spec (decisions 14-19, "Chunking implementation plan" above) are both
settled; `chunk.py` and `heading_agent.py` are the next files to build.
</content>
<parameter name="i">Write merged single-file domain plan