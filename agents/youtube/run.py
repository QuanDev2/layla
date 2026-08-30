"""YouTube transcript extraction tool.

In: video URL (any standard YouTube link form).
Out: transcript segments with per-line timestamps, plus a timestamped
     plain-text rendering, via invoke() or CLI stdout as JSON.
State: with --out-dir, writes <out-dir>/<video_id>/transcript.md.
"""

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
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
_TAG_RE = re.compile(r"<[^>]*>")


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


def _dedupe_segments(segments: list) -> list:
    """Collapse repeated caption text into single segments.

    Handles both artifacts seen in real caption tracks: exact cues repeated
    across segment boundaries, and rolling auto-captions where each cue
    restates the previous one plus new words.

    In: segments in chronological order.
    Out: new list; each kept segment holds the earliest start of its group and
         a duration extended to the group's end.
    """
    kept = []
    for seg in segments:
        text = seg["text"]
        if not text:
            continue

        if kept:
            previous = kept[-1]
            last_text = previous["text"]
            end = seg["start"] + seg["duration"]

            # exact repeat, or this cue is contained in what we already have
            if text == last_text or text in last_text:
                previous["duration"] = round(
                    max(end - previous["start"], previous["duration"]), 3
                )
                continue

            # rolling caption: this cue supersedes the previous one
            if text.startswith(last_text):
                previous["text"] = text
                previous["duration"] = round(end - previous["start"], 3)
                continue

        kept.append(dict(seg))

    return kept


def _parse_vtt(vtt: str) -> list:
    """Parse WebVTT cues into transcript segments.

    In: vtt file text.
    Out: [{start, duration, timestamp, text}] in file order, not deduped.
    """
    segments = []
    lines = vtt.splitlines()
    idx = 0

    while idx < len(lines):
        line = lines[idx].strip()
        if "-->" not in line:
            idx += 1
            continue

        stamps = line.split("-->")
        start = _parse_vtt_timestamp(stamps[0].strip())
        # cue settings (align/position) may trail the end stamp
        end = _parse_vtt_timestamp(stamps[1].strip().split()[0])
        idx += 1

        cue = []
        while idx < len(lines) and lines[idx].strip():
            cue.append(_TAG_RE.sub("", lines[idx]).strip())
            idx += 1
        text = " ".join(" ".join(part for part in cue if part).split())

        if not text or start is None or end is None:
            continue

        segments.append(
            {
                "start": start,
                "duration": round(end - start, 3),
                "timestamp": _format_timestamp(start),
                "text": text,
            }
        )

    return segments


def _parse_vtt_timestamp(stamp: str) -> Optional[float]:
    """Convert a WebVTT HH:MM:SS.mmm stamp to seconds; None when malformed."""
    parts = stamp.replace(",", ".").split(":")
    try:
        if len(parts) == 3:
            hours, minutes, seconds = parts
        elif len(parts) == 2:
            hours, minutes, seconds = "0", parts[0], parts[1]
        else:
            return None
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    except ValueError:
        return None


def _fetch_via_ytdlp(video_id: str, languages: list) -> dict:
    """Fetch a transcript through yt-dlp subtitles.

    In: video id, preferred language codes.
    Out: dict with ok flag; on success language_code, is_generated, segments.
    State: writes subtitle files to a temp dir, removed before returning.
    """
    url = f"https://www.youtube.com/watch?v={video_id}"
    with tempfile.TemporaryDirectory() as tmp:
        cmd = [
            "yt-dlp",
            "--skip-download",
            "--write-subs",
            "--write-auto-subs",
            "--sub-langs",
            ",".join(languages),
            "--sub-format",
            "vtt",
            "--no-warnings",
            "-o",
            f"{tmp}/%(id)s.%(ext)s",
            url,
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        except FileNotFoundError:
            return {"ok": False, "error": "yt-dlp not found on PATH"}
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "yt-dlp timed out after 180s"}

        found = sorted(Path(tmp).glob(f"{video_id}*.vtt"))
        if not found:
            detail = proc.stderr.strip().splitlines()
            return {
                "ok": False,
                "error": (
                    detail[-1]
                    if detail
                    else f"no captions published for {video_id} "
                    f"in {','.join(languages)}"
                ),
            }

        # prefer a manual track: yt-dlp names auto-captions <id>.<lang>.vtt too,
        # so pick the shortest suffix, which is the plain requested language
        chosen = min(found, key=lambda p: len(p.name))
        vtt = chosen.read_text(encoding="utf-8", errors="replace")

    segments = _parse_vtt(vtt)
    if not segments:
        return {"ok": False, "error": f"no cues parsed from {chosen.name}"}

    lang_code = chosen.name[len(video_id) + 1 :].removesuffix(".vtt")
    return {
        "ok": True,
        "language_code": lang_code,
        "is_generated": "Kind: captions" not in vtt.split("\n\n")[0],
        "segments": segments,
    }


def invoke(url: str, languages: Optional[list] = None, **_kwargs) -> dict:
    """Fetch a YouTube transcript with timestamps.

    Tries youtube-transcript-api first, then falls back to yt-dlp subtitles,
    which survives the IP blocks that hit the transcript API.

    In: url, optional preferred languages (default ["en"]).
    Out: dict with ok flag; on success: video_id, language, language_code,
         is_generated, source, segments ([{start, duration, timestamp, text}]),
         and text (segments joined as "[MM:SS] line" per line).
    """
    try:
        video_id = extract_video_id(url)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    langs = languages or ["en"]
    errors = {}

    api = YouTubeTranscriptApi()
    try:
        fetched = api.fetch(video_id, languages=langs)
        segments = [
            {
                "start": snippet.start,
                "duration": snippet.duration,
                "timestamp": _format_timestamp(snippet.start),
                # captions carry hard line breaks; one segment must stay one
                # line so "[MM:SS] text" remains parseable
                "text": " ".join(snippet.text.split()),
            }
            for snippet in fetched
        ]
        segments = [s for s in segments if s["text"]]
        meta = {
            "language": fetched.language,
            "language_code": fetched.language_code,
            "is_generated": fetched.is_generated,
            "source": "youtube-transcript-api",
        }
    except YouTubeTranscriptApiException as exc:
        # str(exc) ends in GitHub-issue boilerplate; .cause is the real reason
        cause = getattr(exc, "cause", None)
        errors["youtube-transcript-api"] = (
            str(cause).strip() if cause else str(exc).strip().splitlines()[0]
        )
        fallback = _fetch_via_ytdlp(video_id, langs)
        if not fallback.get("ok"):
            errors["yt-dlp"] = fallback.get("error", "unknown failure")
            return {"ok": False, "video_id": video_id, "url": url, "errors": errors}
        segments = fallback["segments"]
        meta = {
            "language": fallback["language_code"],
            "language_code": fallback["language_code"],
            "is_generated": fallback["is_generated"],
            "source": "yt-dlp",
        }

    # both sources emit repeated cues; one dedupe policy covers each
    segments = _dedupe_segments(segments)
    text = "\n".join(f"[{s['timestamp']}] {s['text']}" for s in segments)
    result = {
        "ok": True,
        "video_id": video_id,
        "url": url,
        **meta,
        # survives the --out-dir stdout trim, so callers still see the size
        "segment_count": len(segments),
    }
    if errors:
        result["fallback_from"] = errors
    result["segments"] = segments
    result["text"] = text
    return result


def write_transcript(result: dict, out_dir: str) -> str:
    """Write transcript.md for a fetched result.

    In: successful invoke() result, output root.
    Out: path written.
    State: creates <out_dir>/<video_id>/transcript.md.
    """
    video_dir = Path(out_dir) / result["video_id"]
    video_dir.mkdir(parents=True, exist_ok=True)
    path = video_dir / "transcript.md"

    frontmatter = "\n".join(
        [
            "---",
            f"video_id: {result['video_id']}",
            f"url: {result['url']}",
            f"language: {result['language']}",
            f"language_code: {result['language_code']}",
            f"is_generated: {str(result['is_generated']).lower()}",
            f"segments: {len(result['segments'])}",
            f"fetched: {dt.date.today().isoformat()}",
            "---",
        ]
    )
    path.write_text(f"{frontmatter}\n\n{result['text']}\n", encoding="utf-8")
    return str(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument("url", help="YouTube video URL")
    parser.add_argument(
        "--languages",
        help="Comma-separated preferred language codes, e.g. en,es",
        default=None,
    )
    parser.add_argument(
        "--out-dir",
        help="Write <out-dir>/<video_id>/transcript.md instead of only stdout",
        default=None,
    )
    args = parser.parse_args()

    languages = args.languages.split(",") if args.languages else None
    result = invoke(url=args.url, languages=languages)

    if result.get("ok") and args.out_dir:
        result["transcript_path"] = write_transcript(result, args.out_dir)

    # stdout stays machine-readable; drop bulk text when it is already on disk
    if result.get("transcript_path"):
        summary = {k: v for k, v in result.items() if k not in ("segments", "text")}
        print(json.dumps(summary, indent=2, ensure_ascii=False))
    else:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
