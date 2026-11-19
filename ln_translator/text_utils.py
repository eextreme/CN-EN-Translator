"""Paragraph handling, raw-text cleanup and chunking."""

from __future__ import annotations

import re

DEFAULT_CLEANUP_PATTERNS = [
    r"https?://\S+",
    r"www\.\S+",
    r"\S+\.(com|net|org|cc|la|info|xyz)\b",
    r"请收藏",
    r"本章未完",
    r"最新章节",
    r"笔趣阁",
    r"天才一秒记住",
    r"手机用户请",
    r"手机阅读",
    r"章节错误",
    r"请记住本书",
    r"加入书签",
    r"求月票|求推荐票|求订阅",
]


def split_paragraphs(text: str) -> list[str]:
    """A paragraph is any non-empty line. Used identically for Chinese and English."""
    return [line.strip() for line in text.splitlines() if line.strip()]


def join_paragraphs(paragraphs: list[str]) -> str:
    return "\n\n".join(p.strip().replace("\n", " ") for p in paragraphs)


def paragraph_index_at_line(text: str, line_number: int) -> int | None:
    """Map a 0-based line number to a paragraph index (None for blank lines)."""
    lines = text.splitlines()
    if line_number < 0 or line_number >= len(lines) or not lines[line_number].strip():
        return None
    return sum(1 for line in lines[:line_number] if line.strip())


def line_of_paragraph(text: str, index: int) -> int | None:
    """Inverse of paragraph_index_at_line."""
    count = -1
    for n, line in enumerate(text.splitlines()):
        if line.strip():
            count += 1
            if count == index:
                return n
    return None


def is_aligned(raw: str, translation: str) -> bool:
    a, b = split_paragraphs(raw), split_paragraphs(translation)
    return bool(a) and len(a) == len(b)


def replace_paragraphs(translation: str, replacements: dict[int, str]) -> str:
    paras = split_paragraphs(translation)
    for i, text in replacements.items():
        if 0 <= i < len(paras):
            paras[i] = text
    return join_paragraphs(paras)


def clean_raw(text: str, patterns: list[str]) -> tuple[str, int]:
    """Remove site watermarks/ads. Lines that become empty are dropped.

    Returns the cleaned text and the number of lines that were changed or removed.
    """
    compiled = []
    for p in patterns:
        p = p.strip()
        if not p:
            continue
        try:
            compiled.append(re.compile(p, re.IGNORECASE))
        except re.error:
            continue
    out, changed = [], 0
    for line in text.splitlines():
        original = line
        # Normalise full-width indentation and stray whitespace.
        line = line.replace("　", " ").replace("\xa0", " ").strip()
        for rx in compiled:
            line = rx.sub("", line)
        line = line.strip()
        if line != original.strip():
            changed += 1
        if line:
            out.append(line)
    return "\n".join(out), changed


def to_simplified(text: str) -> str:
    try:
        import opencc  # type: ignore
    except ImportError as e:  # pragma: no cover - optional dependency
        raise RuntimeError("Install 'opencc-python-reimplemented' to convert Traditional Chinese.") from e
    return opencc.OpenCC("t2s").convert(text)


def chunk_indices(paragraphs: list[str], max_chars: int) -> list[list[int]]:
    """Group paragraph indices into chunks of roughly max_chars characters."""
    chunks: list[list[int]] = []
    current: list[int] = []
    size = 0
    for i, p in enumerate(paragraphs):
        if current and size + len(p) > max_chars:
            chunks.append(current)
            current, size = [], 0
        current.append(i)
        size += len(p)
    if current:
        chunks.append(current)
    return chunks
