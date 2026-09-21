"""Grade heading_agent's proposed headings for essence-vs-topic-label quality.

units + points (from heading_agent.insert_headings) -> render_sections() ->
LLM judge -> one {score, grade, reasoning, excerpt} verdict per heading.

In: units from chunk.segment_units(), points from heading_agent.insert_headings(),
    source kind, model/effort (omp backend only).
Out: {"ok": True, "verdicts": [...], "backend": str} — verdicts is [] when
     points is [] (nothing to grade, no call made). {"ok": False, "error": str,
     "backend": str} on any failure; never raises.
State: the "omp" backend shells out to a headless subprocess and writes to a
       temp directory it also cleans up. Rides the session's own account
       login for "omp"; the "anthropic" backend needs ANTHROPIC_API_KEY.
       Shares the KNOWLEDGE_LLM backend switch with heading_agent.py — one
       setting controls both generation and grading for this domain.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

NAME = "knowledge_heading_judge"
DESCRIPTION = "Grade heading_agent's proposed headings for essence-vs-topic-label quality, with score and evidence."
INPUT_SCHEMA = {
    "units": {"type": "array", "items": "object", "required": True},
    "points": {"type": "array", "items": "object", "required": True},
    "source_kind": {"type": "string", "required": False},
    "model": {"type": "string", "required": False},
    "effort": {"type": "string", "required": False},
}

DEFAULT_MODEL = "anthropic/claude-sonnet-5"
DEFAULT_EFFORT = "medium"
TOOL_NAMES = ["read", "write"]

# Mirrors heading_agent.py's own split: the omp routing string above is
# unrelated to the anthropic SDK's own model id.
_ANTHROPIC_MODEL = "claude-sonnet-4-5"
_KNOWN_BACKENDS = ("omp", "anthropic", "off")

_JUDGE_PROMPT = """You grade headings inserted into a document by a heading-insertion model.
You never rewrite the document; you only judge the headings already assigned to it.

Each heading should distill the essence of the section under it — the specific
insight, argument, or decision it makes — not just name the topic it discusses.

Positional labels ("Section 3", "Continued") and topic labels (naming the
subject without saying anything about it, e.g. "Vector Backend") both FAIL.
Only an essence heading that states what the section actually concludes or
argues PASSES. Example:
  - weak topic label: "Vector Backend" -> strong essence: "Why brute-force
    cosine beats an ANN index here"

You are given a JSON array of sections, each `{"before_unit", "heading", "text"}`.
For each section, write one verdict:
  {"before_unit": N, "score": X.X, "grade": "essence"|"topic_label"|"ungrounded",
   "reasoning": "...", "excerpt": "..."}

Rules:
- score is 1.0-10.0, one decimal place. 9-10 = sharp essence, unmistakably
  specific. 6-8 = essence but vague or partially topic-flavored. 3-5 = a
  topic label dressed as a sentence, or a heading that only loosely fits its
  section. 1-2 = wrong, misleading, or a positional label.
- grade is your categorical call: "essence" (states the section's actual
  claim), "topic_label" (names the subject, says nothing about it), or
  "ungrounded" (reads as essence but the claim is not actually supported by
  the section text — invented, exaggerated, or contradicted). ungrounded is
  a worse failure than topic_label even when it sounds better, because it
  would mislead a reader searching by this heading.
- reasoning is one or two sentences: why this score, in your own words.
- excerpt is a short verbatim quote (under 200 characters) taken directly
  from the section's "text", not from the heading, that backs your
  reasoning — the specific phrase that shows why the heading fits or
  doesn't.
- Write only the JSON array. No prose, no code fence."""


def render_sections(units: list, points: list) -> list:
    """Slice unit text into one entry per point for the judge.

    In: units (from segment_units), points (from insert_headings).
    Out: [{"before_unit", "heading", "text"}], one per point with a valid,
         in-range, non-duplicate before_unit, in before_unit order.
         Malformed points (bad range/type) are skipped — the caller's own
         structural checks flag those. A duplicate before_unit collapses
         every occurrence but the last into a zero-length span (the next
         point starts at the same unit); those are dropped rather than
         handed to the judge as empty text — check_points's "no_dup" flag
         already surfaces the duplicate itself.
    """
    n = len(units)
    valid = sorted(
        (p for p in points if isinstance(p.get("before_unit"), int) and 0 <= p["before_unit"] < n),
        key=lambda p: p["before_unit"],
    )
    bounds = [p["before_unit"] for p in valid] + [n]
    sections = []
    for p, end_idx in zip(valid, bounds[1:]):
        if end_idx <= p["before_unit"]:
            continue
        text = " ".join(u["text"] for u in units[p["before_unit"]:end_idx])
        sections.append({"before_unit": p["before_unit"], "heading": p.get("heading", ""), "text": text})
    return sections


def _judge_omp(sections: list, model: str, effort: str) -> dict:
    """Run the judge prompt through a headless omp subprocess.

    State: writes sections + prompt output to a temp dir, removed on exit.
    """
    with tempfile.TemporaryDirectory() as tmp:
        sections_path = str(Path(tmp) / "sections.json")
        out_path = str(Path(tmp) / "verdicts.json")
        Path(sections_path).write_text(json.dumps(sections, indent=2, ensure_ascii=False))

        cmd = [
            "omp", "-p",
            "--tools", ",".join(TOOL_NAMES),
            "--no-extensions", "--no-skills", "--no-rules",
            "--approval-mode", "yolo",
            "--model", model,
            "--thinking", effort,
            "--no-session",
            "--system-prompt", _JUDGE_PROMPT,
            f"Read {sections_path}. Write the JSON array of verdicts to {out_path} — "
            "write the file even if the array is empty. Do not skip this step.",
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        except FileNotFoundError:
            return {"ok": False, "error": "omp not found on PATH", "backend": "omp"}
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "omp headless call timed out after 180s", "backend": "omp"}

        if not Path(out_path).exists():
            return {
                "ok": False,
                "error": "omp exited without writing the verdicts file",
                "backend": "omp",
                "stderr": proc.stderr[-2000:],
            }
        try:
            verdicts = json.loads(Path(out_path).read_text())
        except json.JSONDecodeError as e:
            return {"ok": False, "error": f"omp wrote invalid JSON: {e}", "backend": "omp"}
    return {"ok": True, "verdicts": verdicts, "backend": "omp"}


def _judge_anthropic(sections: list) -> dict:
    """Run the judge prompt through the anthropic SDK directly.

    State: none of this repo's — a billed API call on the caller's own key.
    """
    try:
        import anthropic
    except ImportError:
        return {"ok": False, "error": "anthropic package not installed", "backend": "anthropic"}

    if not os.environ.get("ANTHROPIC_API_KEY"):
        return {"ok": False, "error": "ANTHROPIC_API_KEY not set", "backend": "anthropic"}

    client = anthropic.Anthropic()
    response = client.messages.create(
        model=_ANTHROPIC_MODEL,
        max_tokens=4000,
        system=_JUDGE_PROMPT,
        messages=[{"role": "user", "content": json.dumps(sections, ensure_ascii=False)}],
    )
    try:
        verdicts = json.loads(response.content[0].text)
    except (json.JSONDecodeError, IndexError, AttributeError) as e:
        return {"ok": False, "error": f"anthropic returned invalid JSON: {e}", "backend": "anthropic"}
    return {"ok": True, "verdicts": verdicts, "backend": "anthropic"}


def grade(units: list, points: list, source_kind: str = "article",
          model: str = DEFAULT_MODEL, effort: str = DEFAULT_EFFORT) -> dict:
    """Grade every heading in points against the sections it introduces.

    In: units, points, source kind (unused today — kept for parity with
        insert_headings and a future transcript-specific rubric), model/effort
        (omp backend only).
    Out: see module docstring. Backend chosen from KNOWLEDGE_LLM env var,
         same switch heading_agent.py reads; default "omp".
    """
    if not points:
        return {"ok": True, "verdicts": [], "backend": "none"}

    backend = os.environ.get("KNOWLEDGE_LLM", "omp")
    if backend == "off":
        return {"ok": False, "error": "KNOWLEDGE_LLM=off", "backend": "off"}
    if backend not in _KNOWN_BACKENDS:
        return {"ok": False, "error": f"unknown KNOWLEDGE_LLM backend: {backend}", "backend": backend}

    sections = render_sections(units, points)
    if backend == "anthropic":
        return _judge_anthropic(sections)
    return _judge_omp(sections, model, effort)


def invoke(units: list, points: list, source_kind: str = "article",
           model: str = DEFAULT_MODEL, effort: str = DEFAULT_EFFORT, **_kwargs) -> dict:
    """Module entrypoint — see grade()."""
    return grade(units, points, source_kind=source_kind, model=model, effort=effort)


def main() -> int:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument("units_json", help="Path to a JSON file containing the units list")
    parser.add_argument("points_json", help="Path to a JSON file containing the points list")
    parser.add_argument("--kind", default="article")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--effort", default=DEFAULT_EFFORT)
    args = parser.parse_args()

    units = json.loads(Path(args.units_json).read_text())
    points = json.loads(Path(args.points_json).read_text())
    result = invoke(units, points, source_kind=args.kind, model=args.model, effort=args.effort)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
