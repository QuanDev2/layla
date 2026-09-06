---
status: review
scope: step 1 + step 2 only (per user)
updated: 2026-09-03
---

# Knowledge chunking — review of steps 1 and 2

Scope: `__init__.py` (package init) and `chunk.py` (unit segmentation)
against the "Chunking implementation plan" in PLAN.md. Steps 3-7
(`heading_agent.py`, `validate_points`, `split_sections`, `split_body`,
`build_prefix`, public API, CLI) are not built and not reviewed here.

## Verdict

Both correct. Step 1 trivially done; step 2 spec-faithful and runs clean
against both fixtures. Two divergences — one justified, one forward-looking
risk for the step 4/5 builder.

## Step 1 — `__init__.py`

Pass. 0 bytes; `python3 -c "import agents.knowledge"` prints `ok`.

## Step 2 — `chunk.py`

| Check | Result |
|---|---|
| Constants | Exact: `TARGET_CHARS` 1800, `MAX_CHARS` 7000, `ANCHOR_WINDOW` 5; `NAME`/`DESCRIPTION` match spec |
| Unit shape | `{index, text, start, end, stamp}`; `text[start:end] == unit["text"]`; index 0-based contiguous |
| Article — headings | 18/18 heading lines isolated as own units, `#` markers intact, 0 embedded heading lines |
| Article — blank-line paragraphs | 0 units spanning an internal blank line |
| Article — sentence/long split | Paragraph `> TARGET_CHARS` -> sentence split; no-blank-line span -> sentence split |
| Transcript | 534 units (plan says ~534), all `stamp` set, strictly increasing, full coverage `0..22106` |
| Transcript — pre-marker drop | 2 chars (`\n\n`) before first `[MM:SS]` dropped, as spec'd |
| Collapsed one-line transcript | Correctly segments (tested) |
| Zero-marker fallback -> article path | All `stamp=None` |
| `_parse_stamp` | `12:34` -> 754, `1:02:03` -> 3723 |

## Divergences (non-blocking)

### 1. Transcript regex differs from spec — justified

Spec's literal `^\[(\d{1,2}:\d{2}(?::\d{2})?)\]\s*(.*)$` is
self-contradictory: `^`/`$` anchors cannot segment "several `[MM:SS]`
markers in a single line", which the same paragraph claims it handles.
The code drops the anchors and slices between match starts, which does
handle the collapsed case. Code implements the spec's stated intent, not
its buggy regex.

### 2. Blank lines after headings are dropped from the units list

`_segment_generic` skips whitespace-only spans, so the article fixture has
4 `\n\n` gaps — all "blank line after a heading, before body" (verified
offsets 56, 384, 2021, 11932). Not a chunk-output bug: step 5 slices body
by `text[first.start:last.end]`, recovering the whitespace. But step 4's
`structured_text` ("units re-joined in order") will silently lose those
blank lines if built by concatenating `unit["text"]`. Whoever builds
step 4 must offset-slice too; the plan's "re-joining cannot drop text"
claim only holds under offset-slicing. This is the one real forward risk.

## Minor notes

- `_FENCE_RE` untested against real data — the Lessons fixture has 0 code
  fences, so the atomic-fence guarantee is unverified. Handles only
  3-backtick fences (spec only requires ` ``` `); no 4-backtick, `~~~`, or
  EOF-without-trailing-newline.
- `_parse_stamp` defined but unused in step 2 (consumed by step 5/6).
- `---` horizontal rules become paragraph units (harmless — offset slicing
  preserves them).
- Sentence split is naive re abbreviations (`U.S.`, `e.g.`), but matches
  spec's explicit `[.?!]` + whitespace rule.

## Action before step 4 — resolved

Pin down that `structured_text` is built by offset-slicing, not by
concatenating `unit["text"]` — otherwise finding 2 becomes a real text-loss
bug.

Closed in the step 4-7 build: `apply_points()` slices the source between
consecutive unit offsets, so the 4 dropped `\n\n` gaps are recovered.
Verified byte-for-byte — `structured_text == source` on both article
fixtures under `KNOWLEDGE_LLM=off`, and whitespace-normalized identity
`MATCH` on the transcript with 32 live headings inserted.
