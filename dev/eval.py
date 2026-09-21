"""Manual dev harness: run heading_agent + judge.py once, at whatever model and
effort heading_agent.py (and judge.py) currently default to, across a fixed
test-document set. Writes a human-readable report to eval.md plus one
rendered "document with headings inserted" file per doc, for close reading.

document -> segment_units() -> heading_agent.insert_headings() -> points
         -> check_points()            (deterministic, no LLM — dev sanity
                                        check only; not chunk.py's future
                                        production validate_points)
         -> judge.grade()             (score + grade + reasoning + excerpt
                                        per heading)
         -> render_with_headings()    written to eval_runs/<run_id>/<slug>.md
         -> appended section in eval.md

No config sweep — this is a single-shot manual tuning loop, not
a candidate-comparison sweep. To try a
different model or effort, edit heading_agent.DEFAULT_MODEL / DEFAULT_EFFORT
(or judge's) by hand and rerun.

In: none — test set and model/effort come from module state.
Out: {"ok": True, "run_id": str, "rows": [...], "average_score": float|None}.
State: appends to dev/eval.md and writes
       dev/eval_runs/<run_id>/*.md — both gitignored, dev
       scratch, not source. heading_agent.py and judge.py remain this
       domain's only production modules.
"""

import datetime as dt
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import heading_agent  # noqa: E402
import judge  # noqa: E402
from chunker import MAX_CHARS, segment_units  # noqa: E402

# Transcript fixtures were discarded in the 2026-09-20 flatten; refetch one
# with `python3 youtube.py <url>` and add it here when grading transcripts.
TEST_DOCS = [
    {"path": "data/articles/graph-engineering-2026-guide-openclaw-codex.md", "kind": "article"},
    {"path": "data/articles/3-years-of-graph-engineering-with-langgraph.md", "kind": "article"},
    {"path": "data/articles/train-llm-from-scratch.md", "kind": "article"},
]

_HERE = Path(__file__).resolve().parent
EVAL_MD = _HERE / "eval.md"
RUNS_DIR = _HERE / "eval_runs"


_TS_PREFIX_RE = re.compile(r"^\[\d{1,2}:\d{2}(?::\d{2})?\]\s*")
_PUNCT_RE = re.compile(r"[^\w]+", re.UNICODE)


def _normalize_anchor(s: str) -> str:
    """Word-level normalize for anchor comparison.

    Strips punctuation/markdown decoration per word and lowercases —
    heading_agent's model quotes "verbatim" words, not verbatim
    punctuation (drops **bold**, quotes, sometimes trailing commas).
    """
    words = [_PUNCT_RE.sub("", w).lower() for w in s.split()]
    return " ".join(w for w in words if w)


def _first6(unit: dict) -> str:
    """Normalized first six words of a unit's text, for anchor comparison.

    Strips a leading "[MM:SS]" transcript marker first — it isn't one of
    "the words" the model is asked to quote, but a naive split counts it
    as word one, breaking almost every transcript comparison otherwise.
    """
    text = _TS_PREFIX_RE.sub("", unit["text"])
    return _normalize_anchor(" ".join(text.split()[:6]))


def check_points(units: list, points: list) -> dict:
    """Deterministic structural sanity checks — no LLM, no ground truth.

    Out: {"hard_fail", "anchor_ok_rate", "range_ok", "no_dup",
          "section_cap_ok", "length_ok", "bad_anchors", "over_cap"}.
         Empty points is never a hard fail here — we don't know ground
         truth for these fixtures; whether [] was correct is a judgment
         call for you reading eval.md, not this harness.
    """
    if not points:
        return {
            "hard_fail": False, "anchor_ok_rate": None, "range_ok": True, "no_dup": True,
            "section_cap_ok": True, "length_ok": True, "bad_anchors": [], "over_cap": [],
        }

    n = len(units)
    before_units = [p.get("before_unit") for p in points]
    range_ok = all(isinstance(b, int) and 0 <= b < n for b in before_units)
    no_dup = len(before_units) == len(set(before_units))
    length_ok = all(len(p.get("heading", "")) <= 80 for p in points)

    bad_anchors = []
    for p in points:
        b = p.get("before_unit")
        if isinstance(b, int) and 0 <= b < n:
            expected = _first6(units[b])
            actual = _normalize_anchor(str(p.get("anchor", "")))
            if actual != expected:
                bad_anchors.append(b)
    anchor_ok_rate = 1.0 - len(bad_anchors) / len(points)

    over_cap = []
    section_cap_ok = True
    if range_ok and no_dup:
        ordered = sorted(before_units)
        bounds = [0] + ordered + [n]
        for start_idx, end_idx in zip(bounds[:-1], bounds[1:]):
            if end_idx <= start_idx:
                continue
            span = units[end_idx - 1]["end"] - units[start_idx]["start"]
            if span > MAX_CHARS:
                over_cap.append(start_idx)
                section_cap_ok = False

    hard_fail = not (range_ok and no_dup and length_ok and section_cap_ok and not bad_anchors)
    return {
        "hard_fail": hard_fail, "anchor_ok_rate": anchor_ok_rate, "range_ok": range_ok,
        "no_dup": no_dup, "section_cap_ok": section_cap_ok, "length_ok": length_ok,
        "bad_anchors": bad_anchors, "over_cap": over_cap,
    }


def render_with_headings(units: list, points: list) -> str:
    """Reconstruct the document with proposed headings inserted inline.

    Out: markdown text — a "## heading" line (with its anchor as an HTML
         comment, for spotting a misplaced insertion) before each unit a
         valid point targets, unit text otherwise, blank-line joined.
    """
    n = len(units)
    by_before_unit = {
        p["before_unit"]: p for p in points
        if isinstance(p.get("before_unit"), int) and 0 <= p["before_unit"] < n
    }
    blocks = []
    for u in units:
        if u["index"] in by_before_unit:
            p = by_before_unit[u["index"]]
            blocks.append(f"## {p.get('heading', '')}\n<!-- anchor: \"{p.get('anchor', '')}\" -->")
        blocks.append(u["text"])
    return "\n\n".join(blocks)


def _slug(path: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", Path(path).stem.lower()).strip("-")


def _score_table(verdicts: list) -> str:
    lines = ["| unit | heading | score | grade | reasoning | excerpt |", "|---|---|---|---|---|---|"]
    for v in verdicts:
        heading = str(v.get("heading", "")).replace("|", "\\|")
        reasoning = str(v.get("reasoning", "")).replace("|", "\\|").replace("\n", " ")
        excerpt = str(v.get("excerpt", "")).replace("|", "\\|").replace("\n", " ")
        score = v.get("score")
        score_str = f"{score:.1f}" if isinstance(score, (int, float)) else str(score)
        lines.append(f"| {v.get('before_unit')} | {heading} | {score_str} | {v.get('grade')} | {reasoning} | {excerpt} |")
    return "\n".join(lines)


def score_document(doc: dict, run_id: str) -> dict:
    """Run one document through heading_agent + judge.py; build its report section.

    Out: {"path", "kind", "ok", ["error"] | ["headings_inserted", "structural",
          "judge_ok", "scores", "doc_average"], "section_md"}.
    State: writes dev/eval_runs/<run_id>/<slug>.md on success.
    """
    path, kind = doc["path"], doc["kind"]
    text = Path(path).read_text()
    units = segment_units(text, kind)

    result = heading_agent.insert_headings(units, kind)
    if not result.get("ok"):
        section_md = f"## {path}\n\n**heading_agent failed:** {result.get('error')}\n"
        return {"path": path, "kind": kind, "ok": False, "error": result.get("error"),
                "scores": [], "doc_average": None, "section_md": section_md}

    points = result["points"]
    structural = check_points(units, points)

    rendered = render_with_headings(units, points)
    slug = _slug(path)
    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / f"{slug}.md").write_text(rendered)

    judge_result = judge.grade(units, points, source_kind=kind)
    verdicts = judge_result.get("verdicts", []) if judge_result.get("ok") else []
    by_before_unit = {p["before_unit"]: p for p in points if isinstance(p.get("before_unit"), int)}
    for v in verdicts:
        v["heading"] = by_before_unit.get(v.get("before_unit"), {}).get("heading", "")

    scores = [v["score"] for v in verdicts if isinstance(v.get("score"), (int, float))]
    doc_average = sum(scores) / len(scores) if scores else None

    lines = [
        f"## {path}",
        "",
        f"kind: `{kind}` — headings inserted: {len(points)} — "
        f"structural hard_fail: **{structural['hard_fail']}** "
        f"(anchor_ok_rate={structural['anchor_ok_rate']}, section_cap_ok={structural['section_cap_ok']})",
        "",
    ]
    if not judge_result.get("ok"):
        lines.append(f"**judge failed:** {judge_result.get('error')}")
    elif verdicts:
        lines.append(_score_table(verdicts))
        lines.append("")
        lines.append(f"**doc average score: {doc_average:.1f}**" if doc_average is not None else "**doc average score: n/a**")
    else:
        lines.append("_no headings to grade._")
    lines.append("")
    lines.append(f"[heading_agent output](eval_runs/{run_id}/{slug}.md)")

    return {
        "path": path, "kind": kind, "ok": True, "headings_inserted": len(points),
        "structural": structural, "judge_ok": judge_result.get("ok"),
        "scores": scores, "doc_average": doc_average, "section_md": "\n".join(lines),
    }


def run_eval(test_docs: list = None) -> dict:
    """Run every test doc once through heading_agent + judge.py, append to eval.md.

    In: optional override of TEST_DOCS.
    Out: {"ok": True, "run_id", "rows", "average_score"}.
    """
    test_docs = test_docs or TEST_DOCS
    run_id = dt.datetime.now().strftime("%Y%m%dT%H%M%S")

    rows = [score_document(doc, run_id) for doc in test_docs]

    all_scores = [s for r in rows for s in r.get("scores", [])]
    average_score = sum(all_scores) / len(all_scores) if all_scores else None

    parts = [
        f"# Eval run {run_id}",
        "",
        f"heading_agent: model={heading_agent.DEFAULT_MODEL}, effort={heading_agent.DEFAULT_EFFORT}\n"
        f"judge: model={judge.DEFAULT_MODEL}, effort={judge.DEFAULT_EFFORT}",
        "",
    ]
    parts.extend(r["section_md"] for r in rows)
    parts.extend([
        "## Run summary",
        "",
        f"**overall average score: {average_score:.1f}**" if average_score is not None else "**overall average score: n/a**",
        f"({len(all_scores)} headings graded across {len(rows)} documents)",
        "",
        "---",
        "",
    ])

    EVAL_MD.parent.mkdir(parents=True, exist_ok=True)
    with open(EVAL_MD, "a") as f:
        f.write("\n\n".join(parts))

    return {"ok": True, "run_id": run_id, "rows": rows, "average_score": average_score}


def main() -> int:
    result = run_eval()
    print(json.dumps(
        {"run_id": result["run_id"], "average_score": result["average_score"], "docs": len(result["rows"])},
        indent=2,
    ))
    print(f"\nWrote {EVAL_MD}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
