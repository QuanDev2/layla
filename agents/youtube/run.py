"""YouTube transcript extraction agent.

In: video URL (any standard YouTube link form).
Out: transcript segments with per-line timestamps, plus a timestamped
     plain-text rendering, via invoke() or CLI stdout as JSON.
"""

import argparse
import json
import re
import sys
from typing import Optional
from urllib.parse import parse_qs, urlparse

from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import YouTubeTranscriptApiException

NAME = "youtube_transcript"
DESCRIPTION = (
    "Extract the transcript (with timestamps) of a YouTube video from its URL."
)
INPUT_SCHEMA = {
    "url": {
        "type": "string",
        "required": True,
        "description": "YouTube video URL (watch, youtu.be, shorts, embed, live).",
    },
    "languages": {
        "type": "array",
        "items": "string",
        "required": False,
        "description": "Preferred transcript languages in priority order, e.g. ['en'].",
    },
}

_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def extract_video_id(url: str) -> str:
    """Pull the 11-char video id out of any standard YouTube URL shape.

    In: full URL or bare id.
    Out: video id string.
    """
    url = url.strip()
    if _VIDEO_ID_RE.match(url):
        return url

    parsed = urlparse(url)
    host = parsed.netloc.lower().removeprefix("www.").removeprefix("m.")

    if host == "youtu.be":
        video_id = parsed.path.lstrip("/").split("/")[0]
    elif host in ("youtube.com", "music.youtube.com"):
        if parsed.path == "/watch":
            video_id = parse_qs(parsed.query).get("v", [""])[0]
        else:
            # /shorts/<id>, /embed/<id>, /live/<id>
            parts = [p for p in parsed.path.split("/") if p]
            video_id = parts[-1] if parts else ""
    else:
        video_id = ""

    if not _VIDEO_ID_RE.match(video_id):
        raise ValueError(f"Could not extract a video id from: {url}")
    return video_id


def _format_timestamp(seconds: float) -> str:
    """Render seconds as HH:MM:SS (omits hours when zero)."""
    total = int(seconds)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def invoke(url: str, languages: Optional[list] = None, **_kwargs) -> dict:
    """Fetch a YouTube transcript with timestamps.

    In: url, optional preferred languages (default ["en"]).
    Out: dict with ok flag; on success: video_id, language, language_code,
         is_generated, segments ([{start, duration, text}]), and text
         (segments joined as "[MM:SS] line" per line).
    """
    try:
        video_id = extract_video_id(url)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    api = YouTubeTranscriptApi()
    try:
        fetched = api.fetch(video_id, languages=languages or ["en"])
    except YouTubeTranscriptApiException as exc:
        return {"ok": False, "video_id": video_id, "error": str(exc)}

    segments = [
        {
            "start": snippet.start,
            "duration": snippet.duration,
            "timestamp": _format_timestamp(snippet.start),
            "text": snippet.text,
        }
        for snippet in fetched
    ]
    text = "\n".join(f"[{s['timestamp']}] {s['text']}" for s in segments)

    return {
        "ok": True,
        "video_id": video_id,
        "url": url,
        "language": fetched.language,
        "language_code": fetched.language_code,
        "is_generated": fetched.is_generated,
        "segments": segments,
        "text": text,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument("url", help="YouTube video URL")
    parser.add_argument(
        "--languages",
        help="Comma-separated preferred language codes, e.g. en,es",
        default=None,
    )
    args = parser.parse_args()

    languages = args.languages.split(",") if args.languages else None
    result = invoke(url=args.url, languages=languages)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
