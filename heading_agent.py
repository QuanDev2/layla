"""Propose heading insertion points for a document lacking usable structure.

segment_units() units -> insert_headings() -> {before_unit, anchor, heading}
points, via a pluggable backend (omp headless subagent | anthropic API | off).

In: units from chunk.segment_units(), source kind, model/effort, optional
    retry feedback.
Out: {"ok": True, "points": [...], "backend": str} — points may be empty,
     meaning the document is already well structured. {"ok": False,
     "error": str, "backend": str} on any failure; never raises.
State: the "omp" backend shells out to a headless subprocess and writes to a
       temp directory it also cleans up. Rides the session's own account
       login for "omp"; the "anthropic" backend needs ANTHROPIC_API_KEY.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

NAME = "knowledge_heading_inserter"
DESCRIPTION = "Propose heading insertion points for a document that lacks usable structure."
INPUT_SCHEMA = {
    "path": {"type": "string", "required": True},
    "kind": {"type": "string", "required": False},
    "model": {"type": "string", "required": False},
    "effort": {"type": "string", "required": False},
    "feedback": {"type": "string", "required": False},
}

DEFAULT_MODEL = "anthropic/claude-haiku-5"
DEFAULT_EFFORT = "medium"
TOOL_NAMES = ["read", "write"]
# A 534-unit auto-caption transcript (22k chars) exceeded the original 180s
# on a live run; the whole document is one call, so the ceiling scales with
# document length, not with per-heading work.
TIMEOUT_SECONDS = 600

# The anthropic SDK takes its own model id, unrelated to omp's provider-prefixed
# routing string above — see PLAN.md "Assumptions & contingencies".
_ANTHROPIC_MODEL = "claude-haiku-4-5"
_KNOWN_BACKENDS = ("omp", "anthropic", "off")

_BASE_PROMPT = """You mark section boundaries in a document. You never rewrite, summarize, or
reproduce its text.

The document is given as numbered blocks, each starting with "[N]". A block is one
paragraph, one sentence, one transcript segment, or one code block.

Write a JSON array. Each element marks one heading to insert:
  {"before_unit": N, "anchor": "first six words of block N", "heading": "Section title"}

Rules:
- before_unit is the number of the block the heading goes immediately BEFORE.
- anchor must copy the first six words of block N verbatim, so placement is verifiable.
- heading distils the essence of the section: the specific insight, argument, or
  decision it makes — not just the subject it discusses. Positional labels
  ("Section 3", "Continued", "More details") always fail. A topic label also
  fails even though it names the right subject, because it says nothing about
  it — only an essence-capturing heading, stating what the section actually
  concludes or argues, passes. Examples:
    - weak topic label: "Vector Backend" -> strong essence: "Why brute-force
      cosine beats an ANN index here"
    - weak topic label: "Creator-Verifier Pattern" -> strong essence: "One
      agent writes, another checks — catches errors the writer can't see in
      itself"
  heading is your own words, under 80 characters, never a verbatim quote from
  the text.
- Insert a heading at every genuine topic change, including a new item in a
  named list (one of several patterns, steps, or techniques) — each gets its
  own heading even if the resulting section is far shorter than 1800
  characters. A later search for one specific item must land on a heading
  naming that item, not one naming the whole list.
- The ~1800 character target describes how much of ONE topic to cover before
  the next heading; it is not a floor to hit by merging distinct topics
  together. Never leave a section longer than 7000 characters.
- Existing headings do not excuse you from segmenting the rest of the document —
  apply the topic-change and ~1800/7000-character rules above to every unheaded
  stretch, including everything before the first existing heading. An existing
  heading only covers the content it directly introduces, not what precedes it.
  Write [] only if that process finds nothing left to insert.
- Write only the JSON array. No prose, no code fence, no document text."""

_TRANSCRIPT_ADDENDUM = (
    "Blocks are transcript segments prefixed with [MM:SS]. Topic changes are "
    "where the speaker moves to a new subject."
)

_CHAPTER_PREAMBLE = """# Fixed chapter boundaries

The creator already divided this video into chapters, listed below. Those
boundaries are fixed and already applied — you never propose one, move one, or
write a heading that spans two of them.

Your job inside that structure:
- Every chapter gets exactly one heading at its own first block, listed below
  as "heading required at block N". Every one of those blocks must appear in
  your array — a chapter with no heading is a failed answer.
- The creator's chapter title names a subject ("Fabric Quality"); yours states
  what the section actually argues ("Natural fibers outlast synthetics in a
  wardrobe"). Both are kept, so never repeat the chapter title back.
- A chapter marked LONG runs past the chunk target. Give it extra headings at
  real topic changes inside it, enough that no stretch between two of your
  headings exceeds ~1800 characters.
- A chapter not marked LONG gets its one required heading and nothing more.
- Propose no heading at a block outside the ranges listed below.

Chapters, with inclusive block ranges:"""


def render_units(units: list) -> str:
    """Render the numbered blocks the heading model sees.

    In: units from segment_units().
    Out: one "[N] text" block per unit, blank-line joined. A unit's own
         internal newlines are preserved, so a code block still reads as one.
         Indices are the model's only positional vocabulary — source line
         numbers never appear in its input.
    """
    return "\n\n".join(f"[{u['index']}] {u['text']}" for u in units)


def render_chapters(chapters: list) -> str:
    """Render the fixed chapter list the model must work inside.

    In: [{"title", "first_unit", "last_unit", "chars", "long"}].
    Out: the chapter preamble plus one line per chapter. Empty string for
         an empty list, so a chapterless video adds nothing to the prompt.
    """
    if not chapters:
        return ""
    lines = [_CHAPTER_PREAMBLE]
    required = []
    for chapter in chapters:
        required.append(str(chapter["first_unit"]))
        lines.append(
            f"- \"{chapter['title']}\" — blocks {chapter['first_unit']}"
            f"–{chapter['last_unit']}, {chapter['chars']:,} chars"
            + (" LONG" if chapter.get("long") else "")
            + f" → heading required at block {chapter['first_unit']}"
        )
    lines.append(
        f"\nYour array holds at least {len(required)} elements, one at each of "
        f"blocks {', '.join(required)}, plus any extra headings the LONG "
        f"chapters need."
    )
    return "\n".join(lines)


def build_system_prompt(source_kind: str, feedback: Optional[str] = None,
                        chapters: Optional[list] = None) -> str:
    """Build the heading-insertion system prompt.

    In: "article" | "transcript", optional retry feedback, optional fixed
        chapters.
    Out: prompt string. Pure, no side effects.
    """
    sections = [_BASE_PROMPT]
    if source_kind == "transcript":
        sections.append(_TRANSCRIPT_ADDENDUM)
    if chapters:
        sections.append(render_chapters(chapters))
    if feedback:
        sections.append(
            "# Previous attempt failed\n" + feedback
            + "\nFix exactly these issues. Change nothing else."
        )
    return "\n\n".join(sections)


def _insert_headings_omp(prompt: str, rendered: str, model: str, effort: str) -> dict:
    """Run the heading-insertion prompt through a headless omp subprocess.

    State: writes rendered units + prompt output to a temp dir, removed on exit.
    """
    with tempfile.TemporaryDirectory() as tmp:
        units_path = str(Path(tmp) / "units.txt")
        out_path = str(Path(tmp) / "points.json")
        Path(units_path).write_text(rendered)

        cmd = [
            "omp", "-p",
            "--tools", ",".join(TOOL_NAMES),
            "--no-extensions", "--no-skills", "--no-rules",
            "--approval-mode", "yolo",
            "--model", model,
            "--thinking", effort,
            "--no-session",
            "--system-prompt", prompt,
            f"Read {units_path}. Write the JSON array to {out_path} — write "
            "the file even if the array is empty. Do not skip this step.",
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT_SECONDS)
        except FileNotFoundError:
            return {"ok": False, "error": "omp not found on PATH", "backend": "omp"}
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": f"omp headless call timed out after {TIMEOUT_SECONDS}s", "backend": "omp"}

        if not Path(out_path).exists():
            return {
                "ok": False,
                "error": "omp exited without writing the points file",
                "backend": "omp",
                "stderr": proc.stderr[-2000:],
            }
        try:
            points = json.loads(Path(out_path).read_text())
        except json.JSONDecodeError as e:
            return {"ok": False, "error": f"omp wrote invalid JSON: {e}", "backend": "omp"}
    return {"ok": True, "points": points, "backend": "omp"}


def _insert_headings_anthropic(prompt: str, rendered: str) -> dict:
    """Run the heading-insertion prompt through the anthropic SDK directly.

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
        max_tokens=2000,
        system=prompt,
        messages=[{"role": "user", "content": rendered}],
    )
    try:
        points = json.loads(response.content[0].text)
    except (json.JSONDecodeError, IndexError, AttributeError) as e:
        return {"ok": False, "error": f"anthropic returned invalid JSON: {e}", "backend": "anthropic"}
    return {"ok": True, "points": points, "backend": "anthropic"}


def insert_headings(units: list, source_kind: str, model: str = DEFAULT_MODEL,
                     effort: str = DEFAULT_EFFORT, feedback: Optional[str] = None,
                     chapters: Optional[list] = None) -> dict:
    """Propose heading insertion points for a segmented document.

    In: units from segment_units() (caller owns segmentation), source_kind
        ("article" | "transcript"), model/effort (omp backend only), optional
        retry feedback, optional fixed chapters the headings must nest inside.
    Out: see module docstring. Backend chosen from KNOWLEDGE_LLM env var,
         default "omp"; "off" and unrecognized values fail without a call.
    """
    backend = os.environ.get("KNOWLEDGE_LLM", "omp")
    if backend == "off":
        return {"ok": False, "error": "KNOWLEDGE_LLM=off", "backend": "off"}
    if backend not in _KNOWN_BACKENDS:
        return {"ok": False, "error": f"unknown KNOWLEDGE_LLM backend: {backend}", "backend": backend}

    prompt = build_system_prompt(source_kind, feedback, chapters)
    rendered = render_units(units)

    if backend == "anthropic":
        return _insert_headings_anthropic(prompt, rendered)
    return _insert_headings_omp(prompt, rendered, model, effort)


def invoke(path: str, kind: str = "article", model: str = DEFAULT_MODEL,
           effort: str = DEFAULT_EFFORT, feedback: Optional[str] = None, **_kwargs) -> dict:
    """Segment a document from disk and propose heading insertion points for it.

    In: document path, source kind, model/effort, optional retry feedback.
    Out: dict; see insert_headings(). A missing or empty file fails the same
         way, without a call. Never raises.
    State: read-only.
    """
    # Local import: chunk.py imports this module for its own retry orchestration
    # (PLAN.md step 4); importing chunk at module load time here would cycle.
    from chunker import segment_units

    try:
        text = Path(path).read_text()
    except FileNotFoundError:
        return {"ok": False, "error": f"file not found: {path}"}
    if not text.strip():
        return {"ok": False, "error": "empty document"}

    units = segment_units(text, kind)
    return insert_headings(units, kind, model=model, effort=effort, feedback=feedback)


def main() -> int:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument("path", help="Document to propose headings for")
    parser.add_argument("--kind", choices=("article", "transcript"), default="article")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--effort", default=DEFAULT_EFFORT)
    parser.add_argument("--feedback", default=None)
    args = parser.parse_args()

    result = invoke(args.path, kind=args.kind, model=args.model, effort=args.effort, feedback=args.feedback)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
