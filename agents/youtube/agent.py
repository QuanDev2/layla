"""Summarize one YouTube transcript via a headless omp subagent.

In: transcript path, summary output path, model + effort (swappable), optional
    frontmatter block and retry feedback.
Out: JSON {ok, model, effort, summary_path}; never returns the summary content,
     only writes summary_path.
State: writes summary_path. Rides the session's own account login; no separate
        API key or billing.
"""

import argparse
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path
from typing import Optional

NAME = "youtube_summarizer_agent"
DESCRIPTION = "Summarize a fetched YouTube transcript into the domain's summary.md contract."
INPUT_SCHEMA = {
    "transcript_path": {"type": "string", "required": True},
    "summary_path": {"type": "string", "required": True},
    "model": {"type": "string", "required": False},
    "effort": {"type": "string", "required": False},
    "feedback": {"type": "string", "required": False},
    "frontmatter": {"type": "string", "required": False},
}

DEFAULT_MODEL = "anthropic/claude-sonnet-5"
DEFAULT_EFFORT = "medium"

# One subagent, one job: read the transcript, write the summary. Never grant
# `task` — this subagent must not spawn further subagents. See PLAN.md.
TOOL_NAMES = ["read", "write"]

SUMMARY_RULES = """After the frontmatter, write exactly these three sections:

## Summary
Two to four sentences. Neutral description of what the video covers.

## Key points
- [MM:SS] Point, with the timestamp it occurs at.

## Visual references
- [MM:SS] Moment where the speaker points at something the transcript alone cannot convey (diagram, chart, slide, demo).

Rules:
- Cite only timestamps that literally appear in the transcript you read.
- Never write a worth-watching verdict; store a neutral summary only.
- Write nothing before the frontmatter and nothing after the three sections."""


def _parse_frontmatter(text: str) -> dict:
    """Extract flat YAML-ish frontmatter.

    In: file text starting with '---'.
    Out: dict of key -> string value; empty when no block.
    """
    if not text.startswith("---"):
        return {}
    out = {}
    for line in text.splitlines()[1:]:
        if line.strip() == "---":
            break
        if ":" in line:
            key, _, value = line.partition(":")
            out[key.strip()] = value.strip()
    return out


def _render_frontmatter(video_id, url, title, channel, duration) -> str:
    """Render summary frontmatter block from fields."""
    return "\n".join(
        [
            "---",
            f"video_id: {video_id}",
            f"url: {url}",
            f"title: {title}",
            f"channel: {channel}",
            f"duration: {duration}",
            f"added: {dt.date.today().isoformat()}",
            "status: new",
            "---",
        ]
    )


def derive_frontmatter(transcript_path: str) -> str:
    """Build a minimal summary frontmatter from a transcript's own frontmatter.

    In: transcript.md path.
    Out: YAML block with video_id/url copied from the transcript; title/channel/
         duration empty. ingest.py supplies the full block when it has metadata.
    """
    meta = _parse_frontmatter(Path(transcript_path).read_text(encoding="utf-8"))
    return _render_frontmatter(meta.get("video_id", ""), meta.get("url", ""), "", "", "")


def build_system_prompt(frontmatter: str, feedback: Optional[str] = None) -> str:
    """Build the subagent system prompt.

    In: exact frontmatter block, optional retry feedback.
    Out: prompt string. Pure, no side effects.
    """
    sections = [
        "You summarize a single YouTube transcript into a fixed markdown contract. "
        f"Your only tools are: {', '.join(TOOL_NAMES)}.",
        "Start the file with exactly this frontmatter, character for character:\n\n"
        f"```markdown\n{frontmatter}\n```",
        SUMMARY_RULES,
    ]
    if feedback:
        sections.append(
            "# Previous attempt failed\n" + feedback
            + "\nFix exactly these issues. Change nothing else."
        )
    return "\n\n".join(sections)


def invoke(transcript_path, summary_path, model=DEFAULT_MODEL,
           effort=DEFAULT_EFFORT, feedback=None, frontmatter=None, **_kwargs) -> dict:
    """Summarize a transcript into summary_path via a headless omp call.

    In: transcript path, summary output path, model/effort, optional feedback
        and frontmatter block.
    Out: dict with ok flag; never raises for expected failures.
    State: writes summary_path.
    """
    frontmatter = frontmatter or derive_frontmatter(transcript_path)
    prompt = build_system_prompt(frontmatter, feedback)

    cmd = [
        "omp", "-p",
        "--tools", ",".join(TOOL_NAMES),
        "--no-extensions", "--no-skills", "--no-rules",
        "--approval-mode", "yolo",
        "--model", model,
        "--thinking", effort,
        "--no-session",
        "--system-prompt", prompt,
        f"Read {transcript_path}. Write the summary to {summary_path}.",
    ]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    except FileNotFoundError:
        return {"ok": False, "error": "omp not found on PATH"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "omp headless call timed out after 180s"}

    if not Path(summary_path).exists():
        return {
            "ok": False,
            "error": "omp exited without writing summary_path",
            "stderr": proc.stderr[-2000:],
        }
    return {"ok": True, "model": model, "effort": effort, "summary_path": summary_path}


def main() -> int:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument("transcript_path", help="Path to transcript.md")
    parser.add_argument("summary_path", help="Where to write summary.md")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--effort", default=DEFAULT_EFFORT)
    parser.add_argument("--feedback", default=None)
    parser.add_argument(
        "--frontmatter",
        default=None,
        help="Exact YAML frontmatter block; derived from the transcript when omitted",
    )
    args = parser.parse_args()

    result = invoke(
        transcript_path=args.transcript_path,
        summary_path=args.summary_path,
        model=args.model,
        effort=args.effort,
        feedback=args.feedback,
        frontmatter=args.frontmatter,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
