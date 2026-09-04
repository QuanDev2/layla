"""Compare heading_agent model/effort configs on a fixed document set.

document -> segment_units() -> insert_headings() -> points
         -> check_points()     (deterministic: anchor/range/length/cap)
         -> judge_essence()    (LLM judge: essence vs topic label vs ungrounded)
         -> one scored row per (model_config, document)

Two independent signals, not one blended score. Deterministic checks catch
the failures that break the downstream pipeline silently (a wrong anchor
means split_body can't find the insertion point at all — see
pipeline/PLAN.md step 4). The judge catches the one thing the prompt asks
for that code can't verify: whether a heading states the section's actual
claim or just names its subject.

In: candidate "model:effort" strings, list of {"path", "kind", "expect_empty"}
    document specs.
Out: {"ok": True, "rows": [...], "note": str} — one row per config x document.
     A row's "structural" and "judge" sub-dicts are independently gradeable;
     "hard_fail" is True if either flags a problem serious enough that the
     row shouldn't count as a pass regardless of essence_rate.
State: shells out to omp (via heading_agent's own backend) for generation,
       and calls the Anthropic SDK directly for judging — a separate,
       stronger model than the cheap one under test, so it isn't grading
       its own homework. Needs ANTHROPIC_API_KEY for the judge regardless
       of which backend KNOWLEDGE_LLM selects for generation.
"""

import argparse
import json
import os
from pathlib import Path
from typing import Optional

from agents.knowledge import heading_agent
from agents.knowledge.chunk import MAX_CHARS, segment_units

NAME = "knowledge_heading_eval"
DESCRIPTION = "Run heading_agent across candidate model/effort configs against test documents; score structurally and by LLM judge."
INPUT_SCHEMA = {
    "model_configs": {"type": "array", "items": "string", "required": False},
    "test_docs": {"type": "array", "items": "object", "required": False},
}

# Current production default (heading_agent.DEFAULT_MODEL / DEFAULT_EFFORT).
# Add more "model:effort" entries here to compare candidates — that's the
# whole point of this harness, per decision to hold effort at "low" for now
# and sweep it later.
DEFAULT_CANDIDATES = ["anthropic/claude-haiku-5:low"]

# Separate from whatever's under test, deliberately — grading essence
# quality with the same cheap model that produced it is a known blind spot.
_JUDGE_MODEL = "claude-sonnet-5"

# Extend this list as more articles/transcripts come in. "expect_empty"
# marks a doc that already has real headings covering its topics — the
# pass condition there is the model declining to add more, not an empty
# result to be suspicious of (pipeline/PLAN.md step 3, acceptance check 2).
DEFAULT_TEST_DOCS = [
    {
        "path": "agents/knowledge/data/LESSONS-ai-native-sdlc-playbook.md",
        "kind": "article",
        "expect_empty": True,
    },
    {
        "path": "agents/knowledge/data/LESSONS-graph-engineering-2026-guide.md",
        "kind": "article",
        "expect_empty": True,
    },
    {
        "path": "agents/knowledge/data/LESSONS-3-years-graph-engineering-langgraph.md",
        "kind": "article",
        "expect_empty": True,
    },
    {
        "path": "agents/youtube/data/videos/ow1we5PzK-o/transcript.md",
        "kind": "transcript",
        "expect_empty": False,
    },
    {
        "path": "agents/youtube/data/videos/XvmixEXPT3Q/transcript.md",
        "kind": "transcript",
        "expect_empty": False,
    },
    {
        "path": "agents/youtube/data/videos/kVXp6UNVPTo/transcript.md",
        "kind": "transcript",
        "expect_empty": False,
    },
    {
        "path": "agents/youtube/data/videos/4biXYSNkn9Y/transcript.md",
        "kind": "transcript",
        "expect_empty": False,
    },
    {
        "path": "agents/youtube/data/videos/IMLwvK08JVc/transcript.md",
        "kind": "transcript",
        "expect_empty": False,
    },
]

_JUDGE_PROMPT = """You grade headings inserted into a document by a heading-insertion model.

Each heading should distill the essence of the section under it — the specific
insight, argument, or decision it makes — not just name the topic it discusses.

Positional labels ("Section 3", "Continued") and topic labels (naming the
subject without saying anything about it, e.g. "Vector Backend") both FAIL.
Only an essence heading that states what the section actually concludes or
argues PASSES. Example:
  - weak topic label: "Vector Backend" -> strong essence: "Why brute-force
    cosine beats an ANN index here"

For each section below, classify its heading:
- "essence": states the section's specific claim/insight/decision — PASS
- "topic_label": names the subject but says nothing about it — WARNING
- "ungrounded": reads as essence but the claim is not actually supported by
  the section text (invented, exaggerated, or contradicted) — FAIL, and
  worse than topic_label because it would mislead a reader searching by
  this heading

Sections:
<sections>
{sections_json}
</sections>

Return one verdict per section, keyed by its "before_unit". Keep reasoning
to one short sentence per verdict."""

_JUDGE_SCHEMA = {
    "format": {
        "type": "json_schema",
        "schema": {
            "type": "object",
            "properties": {
                "verdicts": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "before_unit": {"type": "integer"},
                            "verdict": {
                                "type": "string",
                                "enum": ["essence", "topic_label", "ungrounded"],
                            },
                            "reasoning": {"type": "string"},
                        },
                        "required": ["before_unit", "verdict", "reasoning"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["verdicts"],
            "additionalProperties": False,
        },
    }
}


def _first6(unit: dict) -> str:
    """Whitespace-normalized first six words of a unit's text."""
    return " ".join(unit["text"].split()[:6])


def check_points(units: list, points: list, expect_empty: bool) -> dict:
    """Deterministic structural checks against the prompt's own invariants.

    In: segmented units, the model's points, whether [] was the right call.
    Out: {"hard_fail": bool, "anchor_ok_rate": float|None, "range_ok": bool,
          "no_dup": bool, "section_cap_ok": bool, "length_ok": bool,
          "bad_anchors": [before_unit, ...], "over_cap": [before_unit, ...]}
         hard_fail is True on any invariant violation, or on emitting points
         when none were wanted, or emitting none when structure already
         existed.
    """
    if expect_empty:
        ok = points == []
        return {
            "hard_fail": not ok,
            "expect_empty_ok": ok,
            "anchor_ok_rate": None,
            "range_ok": True,
            "no_dup": True,
            "section_cap_ok": True,
            "length_ok": True,
            "bad_anchors": [],
            "over_cap": [],
        }

    if not points:
        # A structureless doc that got nothing is its own failure mode,
        # distinct from expect_empty's pass condition.
        return {
            "hard_fail": True,
            "expect_empty_ok": None,
            "anchor_ok_rate": None,
            "range_ok": True,
            "no_dup": True,
            "section_cap_ok": True,
            "length_ok": True,
            "bad_anchors": [],
            "over_cap": [],
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
            actual = " ".join(str(p.get("anchor", "")).split())
            if actual != expected:
                bad_anchors.append(b)
    anchor_ok_rate = 1.0 - len(bad_anchors) / len(points)

    # Section-length cap: only meaningful once every before_unit is
    # in-range and unique — otherwise sorting/spanning is meaningless.
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
                # start_idx identifies the section: the before_unit of the
                # heading governing it, or 0 for the pre-first-heading intro.
                over_cap.append(start_idx)
                section_cap_ok = False

    hard_fail = not (range_ok and no_dup and length_ok and section_cap_ok and not bad_anchors)
    return {
        "hard_fail": hard_fail,
        "expect_empty_ok": None,
        "anchor_ok_rate": anchor_ok_rate,
        "range_ok": range_ok,
        "no_dup": no_dup,
        "section_cap_ok": section_cap_ok,
        "length_ok": length_ok,
        "bad_anchors": bad_anchors,
        "over_cap": over_cap,
    }


def _render_sections(units: list, points: list) -> list:
    """Slice unit text into one entry per point for the judge.

    Out: [{"before_unit", "heading", "text"}], one per point, in
         before_unit order. Malformed points (bad range/type) are skipped —
         check_points already flags those; the judge only sees gradeable
         sections.
    """
    n = len(units)
    valid = sorted(
        (p for p in points if isinstance(p.get("before_unit"), int) and 0 <= p["before_unit"] < n),
        key=lambda p: p["before_unit"],
    )
    bounds = [p["before_unit"] for p in valid] + [n]
    sections = []
    for p, end_idx in zip(valid, bounds[1:]):
        text = " ".join(u["text"] for u in units[p["before_unit"]:end_idx])
        sections.append({"before_unit": p["before_unit"], "heading": p.get("heading", ""), "text": text})
    return sections


def judge_essence(units: list, points: list) -> dict:
    """Grade each heading's essence-vs-topic-label quality with a judge model.

    In: units, the model's points (may be []).
    Out: {"ok": True, "verdicts": [...], "essence_rate": float|None,
          "ungrounded_count": int, "topic_label_count": int}
         essence_rate is None when there's nothing to grade. {"ok": False,
         "error": str} if the judge call itself fails — never raises.
    State: billed Anthropic API call on ANTHROPIC_API_KEY.
    """
    if not points:
        return {"ok": True, "verdicts": [], "essence_rate": None, "ungrounded_count": 0, "topic_label_count": 0}

    try:
        import anthropic
    except ImportError:
        return {"ok": False, "error": "anthropic package not installed"}
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return {"ok": False, "error": "ANTHROPIC_API_KEY not set"}

    sections = _render_sections(units, points)
    prompt = _JUDGE_PROMPT.format(sections_json=json.dumps(sections, indent=2, ensure_ascii=False))

    client = anthropic.Anthropic()
    response = client.messages.create(
        model=_JUDGE_MODEL,
        max_tokens=2000,
        temperature=0,
        messages=[{"role": "user", "content": prompt}],
        output_config=_JUDGE_SCHEMA,
    )
    try:
        verdicts = json.loads(response.content[0].text)["verdicts"]
    except (json.JSONDecodeError, IndexError, AttributeError, KeyError) as e:
        return {"ok": False, "error": f"judge returned invalid JSON: {e}"}

    total = len(verdicts) or 1
    essence_count = sum(1 for v in verdicts if v["verdict"] == "essence")
    ungrounded_count = sum(1 for v in verdicts if v["verdict"] == "ungrounded")
    topic_label_count = sum(1 for v in verdicts if v["verdict"] == "topic_label")
    return {
        "ok": True,
        "verdicts": verdicts,
        "essence_rate": essence_count / total,
        "ungrounded_count": ungrounded_count,
        "topic_label_count": topic_label_count,
    }


def score_document(doc: dict, model: str, effort: str) -> dict:
    """Run one document through one model config and score the result.

    In: {"path", "kind", "expect_empty"}, model id, effort.
    Out: {"path", "model", "effort", "ok", "headings_inserted", "structural",
          "judge"} on success. {"path", "model", "effort", "ok": False,
          "error"} if generation itself failed.
    """
    text = Path(doc["path"]).read_text()
    units = segment_units(text, doc["kind"])
    result = heading_agent.insert_headings(units, doc["kind"], model=model, effort=effort)
    if not result.get("ok"):
        return {"path": doc["path"], "model": model, "effort": effort, "ok": False, "error": result.get("error")}

    points = result["points"]
    structural = check_points(units, points, doc.get("expect_empty", False))
    judge = judge_essence(units, points)
    return {
        "path": doc["path"],
        "model": model,
        "effort": effort,
        "ok": True,
        "headings_inserted": len(points),
        "structural": structural,
        "judge": judge,
    }


def invoke(model_configs: Optional[list] = None, test_docs: Optional[list] = None, **_kwargs) -> dict:
    """Score candidate heading_agent configs across the test document set.

    In: ["model:effort", ...] (effort defaults to heading_agent.DEFAULT_EFFORT
        if omitted), [{"path", "kind", "expect_empty"}, ...].
    Out: {"ok": True, "rows": [...], "note": str}. {"ok": False, "error": str}
         only if the document set is empty — every other failure surfaces as
         a per-row "ok": False rather than aborting the sweep.
    """
    model_configs = model_configs or DEFAULT_CANDIDATES
    test_docs = test_docs or DEFAULT_TEST_DOCS
    if not test_docs:
        return {"ok": False, "error": "no test documents; provide test_docs or populate DEFAULT_TEST_DOCS"}

    rows = []
    for spec in model_configs:
        model, _, effort = spec.partition(":")
        effort = effort or heading_agent.DEFAULT_EFFORT
        for doc in test_docs:
            rows.append(score_document(doc, model, effort))

    return {
        "ok": True,
        "rows": rows,
        "note": "judge model is fixed (claude-sonnet-4-5) regardless of the model column; only generation varies.",
    }


def _print_table(rows: list) -> None:
    header = f"{'model':28} {'effort':7} {'doc':38} {'ok':5} {'headings':8} {'anchor':7} {'essence':8} {'hard_fail':9}"
    print(header)
    print("-" * len(header))
    for r in rows:
        doc_name = Path(r["path"]).name
        if not r["ok"]:
            print(f"{r['model']:28} {r['effort']:7} {doc_name:38} {'ERR':5} {'-':8} {'-':7} {'-':8} {'-':9}  {r.get('error', '')}")
            continue
        s, j = r["structural"], r["judge"]
        anchor = "-" if s["anchor_ok_rate"] is None else f"{s['anchor_ok_rate']:.0%}"
        essence = "-" if not j.get("ok") or j["essence_rate"] is None else f"{j['essence_rate']:.0%}"
        hard_fail = s["hard_fail"] or (j.get("ok") and j["ungrounded_count"] > 0)
        print(f"{r['model']:28} {r['effort']:7} {doc_name:38} {'yes':5} {r['headings_inserted']:8} {anchor:7} {essence:8} {str(hard_fail):9}")


def main() -> int:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument("--model-configs", nargs="+", default=None, help='e.g. "anthropic/claude-haiku-5:low"')
    parser.add_argument("--table", action="store_true", help="print a comparison table instead of raw JSON")
    args = parser.parse_args()

    result = invoke(model_configs=args.model_configs)
    if args.table and result.get("ok"):
        _print_table(result["rows"])
    else:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
