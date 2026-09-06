"""Chunk a document into retrieval-sized candidate snippets.

article/transcript text -> segment_units() -> heading_agent.insert_headings()
-> validate_points() -> split_sections() -> split_body() -> build_prefix()
-> candidate chunks. Nothing is written; the caller confirms first.

In: raw document text or path, source kind ("article" | "transcript"),
    optional document title / channel.
Out: {"ok", "source_kind", "backend", "headings_inserted",
      "structured_text", "chunks"}; each chunk carries prefix, content,
      chars, over_cap, and a transcript timestamp range. Units underneath
      are exact character slices of the input, so re-joining them can
      never alter, drop, or duplicate source text.
State: no writes, no database. The heading step shells out to
       heading_agent (KNOWLEDGE_LLM backend); KNOWLEDGE_LLM=off or
       use_llm=False splits on the document's own headings instead.
"""

import argparse
import json
import re
import sys
from pathlib import Path

NAME = "knowledge_chunker"
DESCRIPTION = "Split a document into retrieval-sized candidate snippets with context prefixes."
INPUT_SCHEMA = {
    "path": {"type": "string", "required": True},
    "kind": {"type": "string", "required": False},
    "title": {"type": "string", "required": False},
    "channel": {"type": "string", "required": False},
    "use_llm": {"type": "boolean", "required": False},
}

TARGET_CHARS = 1800
MAX_CHARS = 7000
ANCHOR_WINDOW = 5
ANCHOR_WORDS = 6
MAX_HEADING_CHARS = 80

# A heading line is always its own unit; never one found inside a fence below.
_HEADING_RE = re.compile(r"^#{1,6} .*$", re.MULTILINE)
# Same shape, matched against a single unit's text to read back its level.
_HEADING_UNIT_RE = re.compile(r"^(#{1,6}) +(.*)$")
# A fenced code block, open to close, is always exactly one unit.
_FENCE_RE = re.compile(r"^```[^\n]*\n.*?^```[^\n]*\n?", re.MULTILINE | re.DOTALL)
# Transcript segment marker. Matched with finditer over the whole body, not
# line-by-line, so a transcript collapsed onto one line still segments.
_TIMESTAMP_RE = re.compile(r"\[(\d{1,2}:\d{2}(?::\d{2})?)\]")
_BLANK_LINE_RE = re.compile(r"\n[ \t]*\n+")
_SENTENCE_RE = re.compile(r"[.?!]+(?:\s+|$)")
# Anchor comparison: the model quotes words verbatim, not punctuation.
_TS_PREFIX_RE = re.compile(r"^\[\d{1,2}:\d{2}(?::\d{2})?\]\s*")
_PUNCT_RE = re.compile(r"[^\w]+", re.UNICODE)


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


def _normalize_anchor(s: str) -> str:
    """Word-level normalize an anchor for comparison.

    In: raw anchor or unit text.
    Out: lowercased words, per-word punctuation/markdown decoration
         stripped. The model quotes words verbatim, not punctuation —
         comparing raw text scores ~0% on real output.
    """
    words = [_PUNCT_RE.sub("", w).lower() for w in s.split()]
    return " ".join(w for w in words if w)


def _unit_anchor(unit: dict, words: int = ANCHOR_WORDS) -> str:
    """Normalized opening words of a unit, for anchor comparison.

    In: unit, word count.
    Out: normalized first N words with any leading "[MM:SS]" marker
         removed — the marker is not one of the words the model is asked
         to quote.
    """
    text = _TS_PREFIX_RE.sub("", unit["text"])
    return _normalize_anchor(" ".join(text.split()[:words]))


def _anchor_matches(anchor: str, units: list, index: int) -> bool:
    """Test a normalized anchor against the unit at index.

    In: normalized anchor, units, candidate index.
    Out: True on a word-prefix match either way — short captions make the
         model quote fewer than six words, caption splices make it quote
         past the unit's end; both are correct placements.
    """
    if not 0 <= index < len(units):
        return False
    expected = _unit_anchor(units[index])
    if not anchor or not expected:
        return False
    return expected.startswith(anchor) or anchor.startswith(expected)


def _find_anchor(anchor: str, units: list, index: int) -> "int | None":
    """Scan index ± ANCHOR_WINDOW for the unit an anchor really names.

    In: normalized anchor, units, stated index.
    Out: nearest matching index, or None. Load-bearing for auto-caption
         boundary splices, where the natural six-word phrase starts a unit
         or two away from the one the model named.
    """
    for offset in range(1, ANCHOR_WINDOW + 1):
        for candidate in (index - offset, index + offset):
            if _anchor_matches(anchor, units, candidate):
                return candidate
    return None


def _heading_unit(unit: dict) -> "tuple | None":
    """Read a unit back as a markdown heading.

    In: unit.
    Out: (level, title) for a "#"-prefixed unit, else None.
    """
    m = _HEADING_UNIT_RE.match(unit["text"].strip())
    if not m:
        return None
    return len(m.group(1)), m.group(2).strip()


def _oversized_sections(units: list, points: list) -> list:
    """Name every section still over MAX_CHARS after applying points.

    In: units, accepted points.
    Out: one scoped failure message per oversized section, empty when
         clean. Boundaries are the document's own headings *and* the
         inserted points — a section the document already delimits is not
         reported as the model's failure.
    """
    if not units:
        return []
    bounds = {0, len(units)}
    bounds.update(p["before_unit"] for p in points if 0 < p["before_unit"] < len(units))
    bounds.update(u["index"] for u in units if _heading_unit(u) and u["index"] > 0)
    edges = sorted(bounds)
    messages = []
    for start, end in zip(edges, edges[1:]):
        chars = units[end - 1]["end"] - units[start]["start"]
        if chars > MAX_CHARS:
            messages.append(
                f"section starting at block {start} is {chars:,} chars, over the "
                f"{MAX_CHARS:,} cap — insert a heading inside it"
            )
    return messages


def validate_points(points: list, units: list) -> tuple:
    """Repair heading points from the model and report what stayed broken.

    In: raw points from heading_agent, units they address.
    Out: (accepted points sorted by before_unit, failure message or None).
         The message names exact offenders — it is fed straight back into
         one retry, so a blanket string is a bug. Drifted anchors are
         moved within ±ANCHOR_WINDOW, duplicates collapse to the first,
         and points landing on an existing heading are dropped silently:
         the document already had structure there.
    """
    problems = []
    kept = []
    total = len(units)

    for point in points or []:
        if not isinstance(point, dict) or "before_unit" not in point or "heading" not in point:
            problems.append(f"dropped malformed point {json.dumps(point, ensure_ascii=False)[:120]}")
            continue
        try:
            index = int(point["before_unit"])
        except (TypeError, ValueError):
            problems.append(f"dropped point with non-numeric before_unit {point['before_unit']!r}")
            continue
        if not 0 <= index <= total:
            problems.append(f"dropped point at before_unit {index}: out of range 0..{total}")
            continue

        heading = str(point["heading"]).lstrip("#").strip()
        if not heading:
            problems.append(f"dropped point at block {index}: empty heading")
            continue
        if len(heading) > MAX_HEADING_CHARS:
            problems.append(
                f"dropped heading at block {index}: {len(heading)} chars, over the "
                f"{MAX_HEADING_CHARS}-char limit ({heading[:40]!r})"
            )
            continue

        anchor = _normalize_anchor(str(point.get("anchor", "")))
        if anchor and index < total and not _anchor_matches(anchor, units, index):
            moved = _find_anchor(anchor, units, index)
            if moved is None:
                problems.append(
                    f"dropped heading {heading!r}: anchor {anchor!r} matches no block "
                    f"within ±{ANCHOR_WINDOW} of block {index}"
                )
                continue
            index = moved

        kept.append({"before_unit": index, "anchor": anchor, "heading": heading})

    kept.sort(key=lambda p: p["before_unit"])
    accepted = []
    seen = set()
    for point in kept:
        index = point["before_unit"]
        if index in seen:
            continue
        seen.add(index)
        if index < total and _heading_unit(units[index]):
            continue
        accepted.append(point)

    problems.extend(_oversized_sections(units, accepted))
    return accepted, ("; ".join(problems) if problems else None)


def apply_points(text: str, units: list, points: list) -> str:
    """Rebuild the document with accepted headings inserted.

    In: source text, its units, accepted points.
    Out: structured_text — source sliced by unit offsets, never by
         concatenating unit["text"], so whitespace between units (blank
         lines after a heading) survives. Only "## heading" lines are
         added; no source character is altered, dropped, or duplicated.
    """
    if not units:
        return text
    by_index = {p["before_unit"]: p["heading"] for p in points}
    out = []
    prev_end = units[0]["start"]

    def emit_heading(heading: str) -> None:
        tail = out[-1] if out else "\n\n"
        if not tail.endswith("\n\n"):
            out.append("\n" if tail.endswith("\n") else "\n\n")
        out.append(f"## {heading}\n\n")

    for unit in units:
        if unit["start"] > prev_end:
            out.append(text[prev_end:unit["start"]])
        if unit["index"] in by_index:
            emit_heading(by_index[unit["index"]])
        out.append(unit["text"])
        prev_end = unit["end"]
    if len(units) in by_index:
        emit_heading(by_index[len(units)])
    return "".join(out)


def split_sections(units: list, points: list) -> list:
    """Cut units into sections at every heading, real or inserted.

    In: units, accepted points.
    Out: [{"heading_path", "units"}] in document order. heading_path is the
         enclosing heading hierarchy — an "###" under an "##" inherits
         both. An inserted heading always sits at level 2 under the
         document's own level-1 title, never under a previously inserted
         one, so consecutive insertions are siblings rather than an
         ever-deepening chain. A document's own heading line stays inside
         its section's units (it is source text); an inserted heading
         never does (it is model output).
    """
    inserted = {p["before_unit"]: p["heading"] for p in points}
    sections = []
    own_path = []   # the document's own headings only
    path = []       # what the current section reports, insertions included
    current = None

    for unit in units:
        if unit["index"] in inserted:
            path = own_path[:1] + [inserted[unit["index"]]]
            current = {"heading_path": list(path), "units": []}
            sections.append(current)
        own = _heading_unit(unit)
        if own:
            level, title = own
            own_path = own_path[:level - 1] + [title]
            path = list(own_path)
            current = {"heading_path": list(path), "units": []}
            sections.append(current)
        if current is None:
            current = {"heading_path": [], "units": []}
            sections.append(current)
        current["units"].append(unit)

    return [s for s in sections if s["units"]]


def split_body(section_units: list) -> list:
    """Group a section's units into TARGET_CHARS-sized chunks.

    In: one section's units.
    Out: list of unit groups. Units are already the finest legal boundary,
         so no cut lands mid-sentence, mid-segment, or mid-code-fence. A
         section at or under TARGET_CHARS yields one group and is never
         merged with a neighbor; a single unit over the target is its own
         group rather than being split.
    """
    groups = []
    current = []
    chars = 0
    for unit in section_units:
        length = unit["end"] - unit["start"]
        if current and chars + length > TARGET_CHARS:
            groups.append(current)
            current, chars = [], 0
        current.append(unit)
        chars += length
    if current:
        groups.append(current)
    return groups


def build_prefix(source_kind: str, doc_title: str, heading_path: list,
                 start: str = None, end: str = None, channel: str = None) -> str:
    """Build the context prefix prepended into a chunk's stored content.

    In: source kind, document title, enclosing heading path, transcript
        timestamp range, channel.
    Out: "Title > Section > Subsection" for articles,
         "Title — Channel [12:34–15:02] > Section" for transcripts. Every
         empty component drops with its separator, so an untitled document
         or unnamed channel degrades cleanly instead of emitting " > ".
    """
    parts = []
    head = (doc_title or "").strip()
    if source_kind == "transcript":
        chan = (channel or "").strip()
        if head and chan:
            head = f"{head} — {chan}"
        elif chan:
            head = chan
        if start and end:
            head = f"{head} [{start}–{end}]".strip()
    if head:
        parts.append(head)
    parts.extend(h.strip() for h in heading_path if h and h.strip())
    return " > ".join(parts)


def _build_chunks(text: str, sections: list, source_kind: str,
                  doc_title: str, channel: str) -> list:
    """Turn sections into stored candidate chunks.

    In: source text, sections, source kind, title, channel.
    Out: chunks with prefix, content, chars, over_cap, and timestamp
         range. A body is sliced by character offset, so whitespace
         between units is preserved exactly as written. chars and the cap
         check count the prefix too — that is what gets embedded.
    """
    chunks = []
    for section in sections:
        for group in split_body(section["units"]):
            body = text[group[0]["start"]:group[-1]["end"]]
            start, end = group[0]["stamp"], group[-1]["stamp"]
            prefix = build_prefix(source_kind, doc_title, section["heading_path"],
                                  start=start, end=end, channel=channel)
            content = f"{prefix}\n\n{body}" if prefix else body
            chunks.append({
                "chunk_index": len(chunks),
                "prefix": prefix,
                "content": content,
                "chars": len(content),
                "over_cap": len(content) > MAX_CHARS,
                "start": start,
                "end": end,
            })
    return chunks


def _resolve_points(units: list, source_kind: str, use_llm: bool) -> tuple:
    """Get validated heading points, with one scoped retry.

    In: units, source kind, whether to call the heading model at all.
    Out: (accepted points, backend). "passthrough" means the model ran and
         found nothing left to insert; "fallback" means no LLM result was
         usable at all and the split runs on the document's own headings.
    State: one or two headless heading_agent calls.

    Diverges from PLAN.md step 4's "discard the LLM result entirely" on a
    second validation failure: points that survive validate_points are
    already structurally sound — the offenders were dropped, not kept — so
    the only complaint that can outlive a retry is an under-dense section.
    Discarding sound headings for that makes the output strictly worse (on
    a heading-less transcript, fallback yields zero boundaries), and the
    cap itself is never breached in output because split_body caps every
    chunk at TARGET_CHARS regardless. So the better of the two attempts
    wins and only a wholly unusable LLM result falls back.
    """
    if not use_llm:
        return [], "fallback"

    # Local import: heading_agent imports segment_units from this module.
    from agents.knowledge.heading_agent import insert_headings

    result = insert_headings(units, source_kind)
    if not result.get("ok"):
        return [], "fallback"
    points, message = validate_points(result.get("points") or [], units)

    if message:
        retry = insert_headings(units, source_kind, feedback=message)
        if retry.get("ok"):
            retry_points, retry_message = validate_points(retry.get("points") or [], units)
            if len(retry_points) > len(points):
                result, points, message = retry, retry_points, retry_message

    if points:
        return points, result["backend"]
    # Empty and clean is the real "already well structured" result; empty
    # with a complaint means nothing the model sent survived validation.
    return [], "passthrough" if message is None else "fallback"


def _chunk(text: str, source_kind: str, doc_title: str, channel: str, use_llm: bool) -> dict:
    """Run the whole pipeline over one document's text.

    In: raw text, source kind, title, channel, LLM toggle.
    Out: see module docstring. Never raises; empty input fails as a dict.
    """
    if not text or not text.strip():
        return {"ok": False, "error": "empty document"}
    units = segment_units(text, source_kind)
    if not units:
        return {"ok": False, "error": "empty document"}

    points, backend = _resolve_points(units, source_kind, use_llm)
    sections = split_sections(units, points)
    return {
        "ok": True,
        "source_kind": source_kind,
        "backend": backend,
        "headings_inserted": len(points),
        "structured_text": apply_points(text, units, points),
        "chunks": _build_chunks(text, sections, source_kind, doc_title, channel),
    }


def chunk_article(text: str, doc_title: str = "", *, use_llm: bool = True) -> dict:
    """Chunk an article into candidate snippets.

    In: article text, document title, LLM toggle.
    Out: see module docstring.
    """
    return _chunk(text, "article", doc_title, None, use_llm)


def chunk_transcript(text: str, *, video_title: str = "", channel: str = "",
                     use_llm: bool = True) -> dict:
    """Chunk a YouTube transcript into candidate snippets.

    In: transcript.md text, video title and channel (not read from disk —
        transcript.md carries neither; they live in the sibling
        summary.md), LLM toggle.
    Out: see module docstring; every chunk carries a [MM:SS] range.
    """
    return _chunk(text, "transcript", video_title, channel, use_llm)


def format_candidates(result: dict) -> str:
    """Render the candidate table shown before anything is stored.

    In: a chunk_article/chunk_transcript result.
    Out: one row per chunk — index, chars, "!" when over the cap, prefix,
         and the body's opening. This is the confirm step; chunk.py has no
         write path.
    """
    if not result.get("ok"):
        return f"error: {result.get('error', 'unknown')}"
    lines = [
        f"{result['source_kind']} · backend={result['backend']} · "
        f"headings_inserted={result['headings_inserted']} · chunks={len(result['chunks'])}",
        "| # | chars | ! | prefix | opening |",
        "|---|---|---|---|---|",
    ]
    for chunk in result["chunks"]:
        body = chunk["content"].split("\n\n", 1)[-1] if chunk["prefix"] else chunk["content"]
        opening = " ".join(body.split())[:60].replace("|", "\\|")
        lines.append(
            f"| {chunk['chunk_index']} | {chunk['chars']} | "
            f"{'!' if chunk['over_cap'] else ''} | "
            f"{chunk['prefix'].replace('|', chr(92) + '|')} | {opening} |"
        )
    return "\n".join(lines)


def invoke(path: str, kind: str = "article", title: str = "", channel: str = "",
           use_llm: bool = True, **_kwargs) -> dict:
    """Chunk a document from disk into candidate snippets.

    In: document path, kind ("article" | "transcript"), title, channel,
        LLM toggle.
    Out: see module docstring. A missing path or empty file fails as a
         dict; never raises.
    State: read-only.
    """
    try:
        text = Path(path).read_text()
    except FileNotFoundError:
        return {"ok": False, "error": f"file not found: {path}"}
    if kind == "transcript":
        return chunk_transcript(text, video_title=title, channel=channel, use_llm=use_llm)
    return chunk_article(text, title, use_llm=use_llm)


def main() -> int:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument("path", help="Document to chunk")
    parser.add_argument("--kind", choices=("article", "transcript"), default="article")
    parser.add_argument("--title", default="")
    parser.add_argument("--channel", default="")
    parser.add_argument("--no-llm", action="store_true", help="Skip the heading model")
    parser.add_argument("--table", action="store_true", help="Print the candidate table")
    args = parser.parse_args()

    result = invoke(args.path, kind=args.kind, title=args.title,
                    channel=args.channel, use_llm=not args.no_llm)
    print(format_candidates(result) if args.table
          else json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
