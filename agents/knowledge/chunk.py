"""Segment a document into atomic units for the knowledge chunker.

article/transcript text -> segment_units() -> ordered list of units, each
with exact character offsets, never derived from source line breaks.

In: raw document text, source kind ("article" | "transcript").
Out: units [{"index", "text", "start", "end", "stamp"}]. index is 0-based
     and contiguous; start/end are char offsets into the input text such
     that text[start:end] == unit["text"]; stamp is the transcript "MM:SS"
     marker or None.
State: none — pure functions, no I/O.
"""

import re

NAME = "knowledge_chunker"
DESCRIPTION = "Split a document into retrieval-sized candidate snippets with context prefixes."

TARGET_CHARS = 1800
MAX_CHARS = 7000
ANCHOR_WINDOW = 5

# A heading line is always its own unit; never one found inside a fence below.
_HEADING_RE = re.compile(r"^#{1,6} .*$", re.MULTILINE)
# A fenced code block, open to close, is always exactly one unit.
_FENCE_RE = re.compile(r"^```[^\n]*\n.*?^```[^\n]*\n?", re.MULTILINE | re.DOTALL)
# Transcript segment marker. Matched with finditer over the whole body, not
# line-by-line, so a transcript collapsed onto one line still segments.
_TIMESTAMP_RE = re.compile(r"\[(\d{1,2}:\d{2}(?::\d{2})?)\]")
_BLANK_LINE_RE = re.compile(r"\n[ \t]*\n+")
_SENTENCE_RE = re.compile(r"[.?!]+(?:\s+|$)")


def _split_keep_seps(pattern, s: str) -> list:
    """Split s at pattern matches, gluing each match onto the piece before it.

    In: separator regex (blank lines, sentence-ending punctuation+space), string.
    Out: (start, end) offsets into s, contiguous, covering all of s, no gaps.
    """
    pieces = []
    prev = 0
    for m in pattern.finditer(s):
        pieces.append((prev, m.end()))
        prev = m.end()
    if prev < len(s):
        pieces.append((prev, len(s)))
    return pieces


def _segment_sentences(text: str, start: int, end: int) -> list:
    """Split text[start:end] into sentence-level units.

    Out: units with stamp=None. A sentence over MAX_CHARS is kept whole —
         never split at an arbitrary character position.
    """
    units = []
    for rel_start, rel_end in _split_keep_seps(_SENTENCE_RE, text[start:end]):
        p_start, p_end = start + rel_start, start + rel_end
        if not text[p_start:p_end].strip():
            continue
        units.append({"text": text[p_start:p_end], "start": p_start, "end": p_end, "stamp": None})
    return units


def _segment_generic(text: str, start: int, end: int) -> list:
    """Split text[start:end] (no headings/fences in range) into units.

    Out: one unit per blank-line-delimited paragraph; a paragraph over
         TARGET_CHARS, or a span with no blank line at all, is sentence-split
         instead. Sentence-level fragments are fine here — split_body (step 5)
         re-accumulates units toward TARGET_CHARS later.
    """
    if not text[start:end].strip():
        return []
    pieces = _split_keep_seps(_BLANK_LINE_RE, text[start:end])
    if len(pieces) == 1:
        return _segment_sentences(text, start, end)
    units = []
    for rel_start, rel_end in pieces:
        p_start, p_end = start + rel_start, start + rel_end
        if not text[p_start:p_end].strip():
            continue
        if (p_end - p_start) > TARGET_CHARS:
            units.extend(_segment_sentences(text, p_start, p_end))
        else:
            units.append({"text": text[p_start:p_end], "start": p_start, "end": p_end, "stamp": None})
    return units


def _segment_article(text: str) -> list:
    """Split text into heading/fence/paragraph/sentence units, in order.

    Out: units with stamp=None. Headings and fenced code blocks are always
         their own unit; a "#" line inside a fence is never treated as one.
    """
    fence_spans = [m.span() for m in _FENCE_RE.finditer(text)]
    heading_spans = [
        m.span() for m in _HEADING_RE.finditer(text)
        if not any(fs <= m.start() < fe for fs, fe in fence_spans)
    ]
    specials = sorted(fence_spans + heading_spans)

    units = []
    pos = 0
    for s, e in specials:
        if s > pos:
            units.extend(_segment_generic(text, pos, s))
        units.append({"text": text[s:e], "start": s, "end": e, "stamp": None})
        pos = e
    if pos < len(text):
        units.extend(_segment_generic(text, pos, len(text)))
    return units


def _parse_stamp(s: str) -> int:
    """Convert 'MM:SS' or 'H:MM:SS' to seconds."""
    seconds = 0
    for part in s.split(":"):
        seconds = seconds * 60 + int(part)
    return seconds


def parse_transcript(text: str) -> tuple:
    """Split a transcript.md body into frontmatter dict and segment units.

    In: full file text, optionally starting with a '---' frontmatter block.
    Out: (frontmatter dict, units). Text before the first [MM:SS] marker is
         dropped. Zero markers found -> falls back to the article path with
         every stamp left None, so a caption-free file still chunks.
    """
    frontmatter = {}
    offset = 0
    if text.startswith("---"):
        lines = text.splitlines(keepends=True)
        offset = len(lines[0])
        for line in lines[1:]:
            offset += len(line)
            if line.strip() == "---":
                break
            if ":" in line:
                key, _, value = line.partition(":")
                frontmatter[key.strip()] = value.strip()

    body = text[offset:]
    matches = list(_TIMESTAMP_RE.finditer(body))
    if not matches:
        units = _segment_article(body)
        for u in units:
            u["start"] += offset
            u["end"] += offset
            u["text"] = text[u["start"]:u["end"]]
        return frontmatter, units

    units = []
    for i, m in enumerate(matches):
        start = offset + m.start()
        end = offset + (matches[i + 1].start() if i + 1 < len(matches) else len(body))
        units.append({"text": text[start:end], "start": start, "end": end, "stamp": m.group(1)})
    return frontmatter, units


def segment_units(text: str, source_kind: str) -> list:
    """Segment a document into atomic, offset-addressed units.

    In: raw text, source_kind ("article" | "transcript").
    Out: units in document order with 0-based contiguous "index" assigned.
    """
    if source_kind == "transcript":
        _frontmatter, units = parse_transcript(text)
    else:
        units = _segment_article(text)
    for i, u in enumerate(units):
        u["index"] = i
    return units
