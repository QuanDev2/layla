"""YouTube playlist/video metadata lister.

In: playlist URL or single video URL.
Out: JSON {ok, count, entries:[{video_id, url, title, channel, duration}]} on stdout.

Wraps `yt-dlp --flat-playlist --dump-json`; no auth needed for public or
unlisted playlists. Private playlists require OAuth and are not supported.
"""

import argparse
import json
import subprocess
import sys

NAME = "youtube_playlist"
DESCRIPTION = "List video ids and metadata for a YouTube playlist or single video URL."
INPUT_SCHEMA = {
    "url": {
        "type": "string",
        "required": True,
        "description": "Playlist URL, or a single video URL (returns one entry).",
    },
    "limit": {
        "type": "integer",
        "required": False,
        "description": "Cap the number of entries returned, newest-first as listed.",
    },
}


def _format_duration(seconds) -> str:
    """Render duration seconds as H:MM:SS or M:SS; empty when unknown."""
    if not seconds:
        return ""
    total = int(seconds)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def invoke(url: str, limit: int = 0, **_kwargs) -> dict:
    """List playlist entries via yt-dlp.

    In: playlist or video URL, optional limit.
    Out: dict with ok flag; on success count + entries with metadata.
    """
    cmd = [
        "yt-dlp",
        "--flat-playlist",
        "--dump-json",
        "--ignore-no-formats-error",
        "--no-warnings",
    ]
    if limit:
        cmd += ["--playlist-end", str(limit)]
    cmd.append(url)

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except FileNotFoundError:
        return {"ok": False, "error": "yt-dlp not found on PATH"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "yt-dlp timed out after 120s"}

    entries = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        video_id = item.get("id") or ""
        if not video_id:
            continue
        entries.append(
            {
                "video_id": video_id,
                "url": item.get("webpage_url") or f"https://www.youtube.com/watch?v={video_id}",
                "title": item.get("title") or "",
                # per-entry channel is absent on channel pages; fall back to
                # the playlist owner, which flat mode does populate
                "channel": (
                    item.get("channel")
                    or item.get("uploader")
                    or item.get("playlist_channel")
                    or item.get("playlist_uploader")
                    or ""
                ),
                "duration": _format_duration(item.get("duration")),
            }
        )

    if not entries:
        return {
            "ok": False,
            "error": (proc.stderr.strip() or "yt-dlp returned no entries"),
        }

    return {"ok": True, "count": len(entries), "entries": entries}


def main() -> int:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument("url", help="Playlist URL or single video URL")
    parser.add_argument("--limit", type=int, default=0, help="Max entries to return")
    args = parser.parse_args()

    result = invoke(url=args.url, limit=args.limit)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
