"""Deterministic YouTube ingest pipeline.

Playlist URL -> playlist.py (diff against disk) -> per video: run.py transcript
+ agent.py summary -> index.py INDEX.md. No LLM reasoning of its own; the one
LLM step (agent.py) is called as a function.

In: playlist URL, optional store root.
Out: JSON {ok, total_in_playlist, skipped_existing, ingested, summary_failed,
     transcript_failed, index_path}.
State: writes transcripts, summaries, and INDEX.md under <root>.
"""

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

from agents.youtube import agent, index, playlist, run

NAME = "youtube_ingest"
DESCRIPTION = "Ingest new videos from a playlist: transcript, summarize, reindex."
INPUT_SCHEMA = {
    "playlist_url": {"type": "string", "required": True},
    "root": {"type": "string", "required": False},
}

DEFAULT_ROOT = str(Path(__file__).resolve().parent / "data" / "videos")


def _render_frontmatter(entry: dict) -> str:
    """Build summary frontmatter from a playlist entry + today's date.

    In: playlist entry (video_id, url, title, channel, duration).
    Out: YAML block; status always new, added always today.
    """
    return "\n".join(
        [
            "---",
            f"video_id: {entry.get('video_id', '')}",
            f"url: {entry.get('url', '')}",
            f"title: {entry.get('title', '')}",
            f"channel: {entry.get('channel', '')}",
            f"duration: {entry.get('duration', '')}",
            f"added: {dt.date.today().isoformat()}",
            "status: new",
            "---",
        ]
    )


def ingest_video(entry: dict, root: str) -> dict:
    """Fetch transcript and summarize one video.

    In: playlist entry (video_id, url, title, channel, duration), store root.
    Out: {video_id, status, ...} — status in ok|transcript_failed|summary_failed.
    State: writes <root>/<id>/transcript.md and summary.md.
    """
    fetch = run.invoke(entry["url"])
    if not fetch.get("ok"):
        return {
            "video_id": entry["video_id"],
            "status": "transcript_failed",
            "error": fetch.get("errors") or fetch.get("error"),
        }

    run.write_transcript(fetch, root)
    video_dir = Path(root) / entry["video_id"]
    transcript_path = str(video_dir / "transcript.md")
    summary_path = str(video_dir / "summary.md")

    result = agent.invoke(transcript_path, summary_path, frontmatter=_render_frontmatter(entry))
    if not result.get("ok"):
        return {
            "video_id": entry["video_id"],
            "status": "summary_failed",
            "error": result.get("error"),
        }
    return {"video_id": entry["video_id"], "status": "ok"}


def invoke(playlist_url: str, root: str = DEFAULT_ROOT, **_kwargs) -> dict:
    """Ingest every not-yet-stored video from a playlist, sequentially.

    In: playlist URL, store root.
    Out: dict with ok flag and per-status breakdown; index always regenerated.
    State: writes under <root>.
    """
    listing = playlist.invoke(playlist_url)
    if not listing.get("ok"):
        return {"ok": False, "error": listing.get("error")}

    entries = listing["entries"]
    root_path = Path(root)
    new_entries = [
        e for e in entries
        if not (root_path / e["video_id"] / "summary.md").exists()
    ]

    results = [ingest_video(e, root) for e in new_entries]

    index_result = index.invoke(root=root)

    return {
        "ok": True,
        "total_in_playlist": len(entries),
        "skipped_existing": len(entries) - len(new_entries),
        "ingested": [r for r in results if r["status"] == "ok"],
        "summary_failed": [r for r in results if r["status"] == "summary_failed"],
        "transcript_failed": [r for r in results if r["status"] == "transcript_failed"],
        "index_path": index_result.get("path"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument("playlist_url", help="YouTube playlist or video URL")
    parser.add_argument("--root", default=DEFAULT_ROOT, help="Video store root")
    args = parser.parse_args()

    result = invoke(playlist_url=args.playlist_url, root=args.root)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
