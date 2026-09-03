---
status: in-progress
updated: 2026-09-01
verify.py: skipped
agent.py: done
ingest.py: done
eval.py: not-started
frames: not-started
data_move: done
docs: done
---

# Implementation plan — agent.py, verify.py, ingest.py, eval.py

Self-contained build plan for a cold-start implementer. Rationale lives in
`/RESEARCH.md`; this file is the "how," not the "why" — read it only if you
need to understand a decision, not to implement one.

## Confirmed decisions (do not re-litigate)
1. Retry cap: **3 total attempts** per video (1 initial + 2 retries).
2. `eval.py` default candidate list: **Anthropic only** for now
   (`anthropic/claude-sonnet-5:medium` — confirmed free, rides the session's
   plan-based login, not pay-per-token). DeepSeek and OpenAI are NOT in the
   default list — DeepSeek's billing origin is unconfirmed (didn't appear in
   `omp usage`'s authenticated-accounts list), OpenAI has no credentials
   configured at all (`No API key found for openai` when tested). Add them
   only after verifying/setting up their credentials.
3. `ingest.py` runs videos **sequentially**, not in parallel. Revisit later —
   `ingest_video()` calls are independent, so parallelizing is a pure
   refactor, not a redesign, when it's worth doing.

## Existing contracts these files build on (verified against current source)

```python
# agents/youtube/playlist.py
def invoke(url: str, limit: int = 0, **_kwargs) -> dict:
    # returns {"ok": bool, "count": int, "entries": [{"video_id", "url", "title", "channel", "duration"}]}
    # or {"ok": False, "error": str}

# agents/youtube/run.py
def invoke(url: str, languages: Optional[list] = None, **_kwargs) -> dict:
    # returns {"ok": True, "video_id", "url", "language", "language_code", "source",
    #          "segment_count", "segments": [...], "text": str}
    # or {"ok": False, "video_id"?, "url"?, "errors": {"youtube-transcript-api": str, "yt-dlp": str}}
def write_transcript(result: dict, out_dir: str) -> str:
    # writes <out_dir>/<video_id>/transcript.md from a successful invoke() result; returns the path written

# agents/youtube/index.py
def invoke(root: str = "data/videos", **_kwargs) -> dict:
    # regenerates <root>/INDEX.md from every */summary.md frontmatter; returns {"ok": True, "count", "path"}
```

Every new file below follows the same tool contract already established:
`NAME`, `DESCRIPTION`, `INPUT_SCHEMA`, `invoke(**kwargs) -> dict` never
raising for expected failures, a `main()` CLI printing JSON to stdout with
exit 0/1.

## Build order

`verify.py` → `agent.py` → `ingest.py` → `eval.py` → update `agents/youtube/AGENTS.md`.

`verify.py` has zero dependencies on the others (pure stdlib, parses two
markdown files) — build and test it standalone first. `agent.py` depends only
on the `omp` CLI, already verified working in this session — see the exact
invocation below, do not re-derive it. `ingest.py` depends on all four other
tools. `eval.py` depends only on `agent.py` + `verify.py`, independent of
`ingest.py` — build last since it's the lower-priority piece (production
ingest matters more than eval infra right now).

---

## 1. `agents/youtube/verify.py` — the Judge

```python
"""Deterministic verifier for a YouTube summary's timestamp citations.

In: summary.md (cited [MM:SS]/[H:MM:SS] timestamps), transcript.md (source).
Out: Verdict — which citations are real, which aren't, scoped feedback text.
"""

NAME = "youtube_summary_verifier"
DESCRIPTION = "Check every timestamp cited in a summary.md against its transcript.md."
INPUT_SCHEMA = {
    "summary_path": {"type": "string", "required": True},
    "transcript_path": {"type": "string", "required": True},
}

def check(summary_path: str, transcript_path: str) -> dict:
    """Returns {"ok": bool, "verified": [str], "failed": [str], "detail": str}."""
```

**Behavior:**
1. Extract every `[MM:SS]` / `[H:MM:SS]` citation from `summary.md`'s body
   (regex `\[(\d{1,2}:\d{2}(?::\d{2})?)\]`, scan the whole file — Key points
   and Visual references sections both cite timestamps per the contract in
   `AGENTS.md`).
2. Parse `transcript.md`: every line matches `^\[(\d{1,2}:\d{2}(?::\d{2})?)\]`
   at the start (per `run.py`'s established format) — collect the full set of
   transcript timestamps as seconds (reuse/mirror the `_parse_vtt_timestamp`
   conversion logic already in `run.py`, or import it directly).
3. For each cited timestamp: check exact match first. **If not exact, check
   within ±2 seconds of any transcript timestamp before declaring failure** —
   real citations were previously observed landing 1 second off due to
   rounding (verified during manual review of a real ingested video); exact
   match alone would cause spurious retries on correct citations.
4. **Zero citations found in `summary.md` is itself a failure** — the
   contract requires "Key points" to carry several timestamped bullets; a
   summary with none has violated the format, not just skipped verification.
5. `detail` must be scoped, not a blanket message — e.g.
   `"2 of 8 citations failed: [08:52] not found within ±2s of any transcript line; [22:10] not found. 6 citations verified."`
   This string is fed directly into `agent.py`'s retry prompt — it must be
   specific enough for the model to fix exactly those citations without
   guessing what else might be wrong (Vercel's scoped-claims contract).

**Acceptance test** (run against the real ingested video,
`data/videos/ow1we5PzK-o/`):
- Craft a `summary.md` copying its real citations → `check()` returns `ok: True`.
- Mutate one citation to a timestamp that doesn't exist (`[99:99]`) →
  `ok: False`, that timestamp named in `failed`, others still in `verified`.
- Strip all citations from the Key points section → `ok: False`, `detail`
  states zero citations found.

---

## 2. `agents/youtube/agent.py` — the Specialist (rename target for the
   previously-discussed `summarize.py`; this is the file, built fresh)

```python
"""Bootstraps a stateless, deterministic subagent that summarizes one
YouTube transcript. The only genuine second-agent-context in this domain —
everything else in agents/youtube/ is a plain function.

In: transcript path, summary output path, model+effort (swappable), optional
    retry feedback from verify.py.
Out: {"ok": bool, "model": str, ...} — does NOT return the summary content,
     only writes it (mirrors run.py's --out-dir convention).
State: writes summary_path. Rides the session's own account login — no
    separate API key, no billing beyond what's already configured.
"""

NAME = "youtube_summarizer_agent"
DESCRIPTION = "Summarize a fetched YouTube transcript into the domain's summary.md contract."
INPUT_SCHEMA = {
    "transcript_path": {"type": "string", "required": True},
    "summary_path": {"type": "string", "required": True},
    "model": {"type": "string", "required": False},
    "effort": {"type": "string", "required": False},
    "feedback": {"type": "string", "required": False},
}

DEFAULT_MODEL = "anthropic/claude-sonnet-5"
DEFAULT_EFFORT = "medium"

SUMMARY_CONTRACT = """<embed the exact summary.md format block from
agents/youtube/AGENTS.md's "summary.md format" section verbatim — frontmatter
field order, Summary/Key points/Visual references sections, the status enum,
and the "never write a worth-watching verdict" rule>"""
```

**`build_system_prompt(tool_names: list[str], feedback: Optional[str]) -> str`**
— pure function, no side effects, unit-testable in isolation:
```python
def build_system_prompt(tool_names, feedback=None):
    sections = [
        "You summarize YouTube transcripts into a fixed markdown contract. "
        f"You have exactly these tools: {', '.join(tool_names)}.",
        SUMMARY_CONTRACT,
        "Cite only timestamps that literally appear in the transcript you read. "
        "Report your work honestly — do not claim a citation is correct without "
        "having read that exact line.",
    ]
    if feedback:
        sections.append(
            f"# Previous attempt failed verification\n{feedback}\n"
            "Fix exactly these issues. Do not change anything else."
        )
    return "\n\n".join(sections)
```
`tool_names` is **always** `["read", "write"]` in this file — **never
`"task"`**. This is a stated rule, not an accidental omission (Vercel's
spawn-permissions finding): this subagent must not be able to spawn further
subagents. Enforced structurally by the `--tools` flag below, restated here
so it can't silently drift if the file is edited later.

**`invoke(...)` — the verified `omp -p` invocation shape (already tested
live in this session, do not re-derive):**
```python
def invoke(transcript_path, summary_path, model=DEFAULT_MODEL,
           effort=DEFAULT_EFFORT, feedback=None, **_kwargs):
    prompt = build_system_prompt(["read", "write"], feedback)
    cmd = [
        "omp", "-p",
        "--tools", "read,write",
        "--no-extensions", "--no-skills", "--no-rules",
        "--approval-mode", "yolo",
        "--model", model,
        "--thinking", effort,
        "--no-session",
        "--system-prompt", prompt,
        f"Read {transcript_path}. Write {summary_path}.",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    except FileNotFoundError:
        return {"ok": False, "error": "omp not found on PATH"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "omp headless call timed out after 180s"}

    if not Path(summary_path).exists():
        return {"ok": False, "error": "omp exited without writing summary_path",
                "stderr": proc.stderr[-2000:]}
    return {"ok": True, "model": model, "effort": effort, "summary_path": summary_path}
```

**Known gotcha, flagged not solved:** cost extraction was NOT nailed down in
this session — a plain-text `-p` call (no `--mode json`) is simplest and
sufficient for v1 (file-exists check is the real success signal). If
`eval.py` needs per-call cost later, that requires `--mode json` and parsing
the streamed `message_end`/`turn_end` events' `usage.cost.total` fields —
observed in manual testing to possibly duplicate the same number across
`message_end` and `turn_end` for one turn. **Do not assume naive summation
works** — verify empirically against a fresh `--mode json` run before trusting
any aggregate.

**Acceptance test:**
- Run against `data/videos/ow1we5PzK-o/transcript.md` with no feedback →
  `verify.check()` on the result returns `ok: True`.
- Run again with a fabricated `feedback` string naming a specific fake
  problem → confirm the resulting `summary.md` visibly changed in response
  (proves the feedback path reaches the model, not just plumbing that's
  wired but ignored).

---

## 3. `agents/youtube/ingest.py` — orchestration glue (SequentialAgent +
   LoopAgent, as plain Python — no framework object needed)

```python
"""Deterministic pipeline: playlist -> diff -> per-video (fetch + verify-loop)
-> index. No LLM reasoning of its own; the one LLM step (agent.py) is called
as a function, its retries governed by verify.py's deterministic judgment.
"""

NAME = "youtube_ingest"
DESCRIPTION = "Ingest new videos from a playlist: transcript, summarize-and-verify, reindex."
INPUT_SCHEMA = {
    "playlist_url": {"type": "string", "required": True},
    "root": {"type": "string", "required": False},
}

MAX_ATTEMPTS = 3  # confirmed: 1 initial + 2 retries
```

```python
def ingest_video(video: dict, root: str) -> dict:
    fetch = run.invoke(video["url"])
    if not fetch.get("ok"):
        return {"video_id": video["video_id"], "status": "transcript_failed",
                "error": fetch.get("errors") or fetch.get("error")}
    run.write_transcript(fetch, root)
    transcript_path = f"{root}/{video['video_id']}/transcript.md"
    summary_path = f"{root}/{video['video_id']}/summary.md"

    feedback = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        result = agent.invoke(transcript_path, summary_path, feedback=feedback)
        if not result.get("ok"):
            feedback = f"Agent invocation failed: {result.get('error')}"
            continue
        verdict = verify.check(summary_path, transcript_path)
        if verdict["ok"]:
            return {"video_id": video["video_id"], "status": "ok", "attempts": attempt}
        feedback = verdict["detail"]

    # exhausted MAX_ATTEMPTS without a passing verdict — honest failure exit,
    # never silently accept a failing summary (Vercel: truthful result, no confabulation)
    return {"video_id": video["video_id"], "status": "needs_review",
            "attempts": MAX_ATTEMPTS, "last_feedback": feedback}


def invoke(playlist_url: str, root: str = "data/videos", **_kwargs) -> dict:
    listing = playlist.invoke(playlist_url)
    if not listing.get("ok"):
        return {"ok": False, "error": listing.get("error")}

    entries = listing["entries"]
    new_videos = [
        e for e in entries
        if not Path(f"{root}/{e['video_id']}/summary.md").exists()
    ]

    results = [ingest_video(v, root) for v in new_videos]  # SEQUENTIAL, confirmed

    index_result = index.invoke(root=root)

    return {
        "ok": True,
        "total_in_playlist": len(entries),
        "skipped_existing": len(entries) - len(new_videos),
        "ingested": [r for r in results if r["status"] == "ok"],
        "needs_review": [r for r in results if r["status"] == "needs_review"],
        "transcript_failed": [r for r in results if r["status"] == "transcript_failed"],
        "index_path": index_result.get("path"),
    }
```

Import `playlist`, `run`, `agent`, `verify`, `index` as sibling modules
(`from agents.youtube import playlist, run, agent, verify, index`) and call
their functions directly — no subprocess, no JSON round-trip, since they're
all in the same package. Only `agent.py` itself shells out to `omp`.

**Acceptance test:**
- Run against the user's real unlisted playlist
  (`https://youtube.com/playlist?list=PLdySlNtwDG0c`) end to end. Known
  ground truth: 1 video (`ow1we5PzK-o`).
- Run it a **second** time immediately after → `skipped_existing: 1`,
  `ingested: []` — proves the diff-against-disk check works.
- Confirm `INDEX.md` reflects the result after both runs.

---

## 4. `agents/youtube/eval.py` — model/provider comparison harness

```python
"""Compare summarizer model configs on a fixed transcript set, scored by
verify.py. Answers "which model should we default to" — distinct from
ingest.py's retry loop, which answers "did this specific run succeed."
"""

NAME = "youtube_agent_eval"
DESCRIPTION = "Run agent.py across candidate models against existing transcripts, score with verify.py."
INPUT_SCHEMA = {
    "model_configs": {"type": "array", "items": "string", "required": False},
    "test_transcripts": {"type": "array", "items": "string", "required": False},
}

# Confirmed decision #2: free-verified only. Do not add deepseek/openai
# entries until their credentials are separately confirmed/set up.
DEFAULT_CANDIDATES = ["anthropic/claude-sonnet-5:medium"]
```

```python
def run_eval(model_configs=None, test_transcripts=None, root="data/videos") -> dict:
    model_configs = model_configs or DEFAULT_CANDIDATES
    # default test set: every transcript already on disk. This doubles as a
    # regression check (re-summarizing known-good videos), not a held-out
    # benchmark — no labeled eval fixtures exist yet. Note this limitation
    # in the output rather than silently presenting it as more rigorous
    # than it is.
    test_transcripts = test_transcripts or sorted(
        str(p) for p in Path(root).glob("*/transcript.md")
    )

    rows = []
    for spec in model_configs:
        model, _, effort = spec.partition(":")
        effort = effort or agent.DEFAULT_EFFORT
        for transcript_path in test_transcripts:
            with tempfile.TemporaryDirectory() as tmp:
                summary_path = f"{tmp}/summary.md"
                result = agent.invoke(transcript_path, summary_path, model=model, effort=effort)
                if result.get("ok"):
                    verdict = verify.check(summary_path, transcript_path)
                else:
                    verdict = {"ok": False, "verified": [], "failed": [], "detail": result.get("error")}
                rows.append({
                    "model": spec, "transcript": transcript_path,
                    "ok": verdict["ok"],
                    "verified_count": len(verdict["verified"]),
                    "failed_count": len(verdict["failed"]),
                })

    return {"ok": True, "rows": rows, "note": "test set is existing ingested transcripts, not held-out fixtures"}
```

**Acceptance test:** run with default candidates against whatever transcripts
exist on disk at build time; confirm a comparison table prints with a
pass/fail count per model, not a crash on an empty test set (handle zero
transcripts gracefully — return `{"ok": False, "error": "no transcripts found; ingest at least one video first"}`).

---

## 5. Update `agents/youtube/AGENTS.md`

Replace the "Workflow: ingest a playlist" section's steps 1–5 (currently:
manual `playlist.py` → skip-check → `run.py` → fan-out-subagents-via-`task` →
`index.py`) with:

```markdown
## Workflow: ingest a playlist

Call `python3 -m agents.youtube.ingest <playlist-url>`. It handles listing,
skip-if-already-ingested, transcript fetch, summarize-and-verify (up to 3
attempts per video, with scoped feedback on retry), and index regeneration
internally — one call, not a manual sequence of steps.

Report to the user: count ingested, titles, anything in `needs_review`
(failed verification 3 times — surface this, don't hide it) or
`transcript_failed` (no captions available).
```

Remove the old "Fan out one subagent per video... single `task` batch"
instruction — that's now internal to `ingest.py`, not something Layla does
by hand.

---

## Explicitly out of scope for this plan
- Frame extraction (still specified in `SPEC.md`, unbuilt, separate effort).
- Email/calendar domains.
- Parallelizing `ingest_video()` calls (deferred per decision #3).
- Adding DeepSeek/OpenAI to `eval.py`'s default candidates (deferred per
  decision #2, until their credentials are separately resolved).
