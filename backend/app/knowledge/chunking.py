"""Split documents into overlapping, heading-aware chunks for retrieval.

Markdown is split at headings first so a chunk rarely spans two procedures; each chunk
records the heading path it sits under ("Runbook > Restarting > Checks"), which is
indexed with extra weight. Long sections are split at paragraph, then line, then word
boundaries with a character overlap so a sentence cut at a boundary is still found.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")


@dataclass(frozen=True, slots=True)
class Chunk:
    index: int
    content: str
    heading: str


def _sections(text: str) -> list[tuple[str, str]]:
    """(heading path, body) pairs; code fences never start a section."""
    path: list[tuple[int, str]] = []
    sections: list[tuple[str, list[str]]] = [("", [])]
    in_fence = False
    for line in text.splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
        m = None if in_fence else _HEADING.match(line)
        if m:
            level, title = len(m.group(1)), m.group(2).strip()
            path = [(lvl, t) for lvl, t in path if lvl < level] + [(level, title)]
            sections.append((" > ".join(t for _, t in path), [line]))
        else:
            sections[-1][1].append(line)
    return [(h, "\n".join(body).strip()) for h, body in sections if "\n".join(body).strip()]


def _split(text: str, size: int, overlap: int) -> list[str]:
    if len(text) <= size:
        return [text]
    pieces: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            window = text[start:end]
            # Prefer a paragraph, then a line, then a word boundary in the last third.
            for sep in ("\n\n", "\n", " "):
                cut = window.rfind(sep, size * 2 // 3)
                if cut > 0:
                    end = start + cut
                    break
        piece = text[start:end].strip()
        if piece:
            pieces.append(piece)
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return pieces


def chunk_text(text: str, *, size: int = 2400, overlap: int = 300) -> list[Chunk]:
    overlap = min(overlap, size // 2)
    chunks: list[Chunk] = []
    buffer: list[str] = []
    buffer_heading = ""

    def flush() -> None:
        nonlocal buffer
        if buffer:
            body = "\n\n".join(buffer)
            for piece in _split(body, size, overlap):
                chunks.append(Chunk(len(chunks), piece, buffer_heading))
            buffer = []

    for heading, body in _sections(text):
        # Merge small neighbouring sections so tiny headings don't become tiny chunks.
        if buffer and sum(len(b) for b in buffer) + len(body) <= size // 2:
            buffer.append(body)
            continue
        flush()
        buffer, buffer_heading = [body], heading
    flush()
    return chunks
