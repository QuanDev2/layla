"""Fetch a YouTube video's captions and metadata for knowledge capture.

video URL -> youtube-transcript-api (fallback: yt-dlp subtitles) -> deduped
segments -> "[MM:SS] line" text, plus title/channel/duration from yt-dlp.

In: video URL (any standard YouTube link form), optional preferred languages.
Out: {"ok", "video_id", "url", "title", "channel", "duration", "language",
     "is_generated", "source", "segment_count", "segments", "text"}; never
     raises. Metadata is best-effort — captions still return without it.
State: with --out, writes the transcript text to that path. Nothing else is
       written; the database is ingest.py's job.
"""

import argparse
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
DESCRIPTION = "Fetch a YouTube video's timestamped captions and metadata."
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


def video_metadata(url: str) -> dict:
    """Read a video's title, channel and duration via yt-dlp.

    In: video URL.
    Out: {"ok": True, "title", "channel", "duration"} or {"ok": False,
         "error": str}. Best-effort — captions do not depend on it.
    """
    cmd = [
        "yt-dlp",
        "--flat-playlist",
        "--dump-json",
        "--ignore-no-formats-error",
        "--no-warnings",
        url,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except FileNotFoundError:
        return {"ok": False, "error": "yt-dlp not found on PATH"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "yt-dlp timed out after 120s"}

    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        return {
            "ok": True,
            "title": item.get("title") or "",
            # per-entry channel is absent on channel pages; the playlist
            # owner is populated there instead
            "channel": (
                item.get("channel")
                or item.get("uploader")
                or item.get("playlist_channel")
                or item.get("playlist_uploader")
                or ""
            ),
            "duration": _format_duration(item.get("duration")),
        }
    return {"ok": False, "error": (proc.stderr.strip() or "yt-dlp returned no metadata")}


def video_chapters(url: str) -> dict:
    """Read a video's published chapters via yt-dlp.

    In: video URL.
    Out: {"ok": True, "chapters": [{"title", "start", "end"}]} with times
         in seconds; an empty list means the creator published none.
         {"ok": False, "error": str} when yt-dlp is missing or fails.
    """
    cmd = [
        "yt-dlp",
        "--skip-download",
        "--dump-json",
        "--playlist-items", "1",
        "--no-warnings",
        url,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except FileNotFoundError:
        return {"ok": False, "error": "yt-dlp not found on PATH"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "yt-dlp timed out after 120s"}

    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        chapters = []
        for chapter in item.get("chapters") or []:
            title = str(chapter.get("title") or "").strip()
            start = chapter.get("start_time")
            if not title or start is None:
                continue
            chapters.append({"title": title, "start": float(start),
                             "end": chapter.get("end_time")})
        return {"ok": True, "chapters": chapters}
    return {"ok": False, "error": (proc.stderr.strip() or "yt-dlp returned no metadata")}


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
    Out: dict with ok flag; on success: video_id, url, title, channel,
         duration, chapters, language, language_code, is_generated,
         source, segment_count, segments ([{start, duration, timestamp,
         text}]), and text (segments joined as "[MM:SS] line" per line).
         Metadata is best-effort: a yt-dlp failure leaves title/channel/
         duration empty and chapters absent, records metadata_error, and
         never fails the fetch.
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
    meta_result = video_metadata(url)
    chapter_result = video_chapters(url)
    result = {
        "ok": True,
        "video_id": video_id,
        "url": url,
        "title": meta_result.get("title", ""),
        "channel": meta_result.get("channel", ""),
        "duration": meta_result.get("duration", ""),
        "chapters": chapter_result.get("chapters", []),
        **meta,
        # survives the --out stdout trim, so callers still see the size
        "segment_count": len(segments),
    }
    if not meta_result.get("ok"):
        result["metadata_error"] = meta_result.get("error", "unknown failure")
    if not chapter_result.get("ok"):
        result["chapters_error"] = chapter_result.get("error", "unknown failure")
    if errors:
        result["fallback_from"] = errors
    result["segments"] = segments
    result["text"] = text
    return result


def write_text(result: dict, path: str) -> str:
    """Write the transcript body to a file for capture.

    In: successful invoke() result, destination path.
    Out: path written.
    State: creates parent directories; writes "[MM:SS] line" text only —
           title/channel/url belong in the documents row, not the file.
    """
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(result["text"] + "\n", encoding="utf-8")
    return str(out)


def main() -> int:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument("url", help="YouTube video URL")
    parser.add_argument(
        "--languages",
        help="Comma-separated preferred language codes, e.g. en,es",
        default=None,
    )
    parser.add_argument(
        "--out",
        help="Write the transcript text to this path; stdout then omits the bulk",
        default=None,
    )
    args = parser.parse_args()

    languages = args.languages.split(",") if args.languages else None
    result = invoke(url=args.url, languages=languages)

    if result.get("ok") and args.out:
        result["transcript_path"] = write_text(result, args.out)

    # stdout stays machine-readable; drop bulk text when it is already on disk
    if result.get("transcript_path"):
        summary = {k: v for k, v in result.items() if k not in ("segments", "text")}
        print(json.dumps(summary, indent=2, ensure_ascii=False))
    else:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
