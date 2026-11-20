"""Chinese → English pop-up dictionary (CC-CEDICT) and the personal word list.

CC-CEDICT (https://cc-cedict.org, CC BY-SA 4.0) is downloaded once into the config folder. Lookup is a
longest-match from the hovered character, like browser pop-up dictionaries: hovering 筑 in 筑基期 shows 筑基期,
then 筑基, then 筑. A longer word that *starts earlier* and covers the hovered character is offered too.

No Qt code here, so it is testable on its own.
"""

from __future__ import annotations

import gzip
import json
import re
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

CEDICT_URL = "https://www.mdbg.net/chinese/export/cedict/cedict_1_0_ts_utf-8_mdbg.txt.gz"
CEDICT_FILENAME = "cedict_ts.u8"

_LINE_RX = re.compile(r"^(\S+) (\S+) \[([^\]]*)\] /(.*)/\s*$")
_SENTENCE_END = "。！？!?…；;\n"
_CLOSERS = "”’」』）)】"

# ------------------------------------------------------------------ pinyin
_TONE_MARKS = {
    "a": "āáǎà", "e": "ēéěè", "i": "īíǐì", "o": "ōóǒò", "u": "ūúǔù", "ü": "ǖǘǚǜ",
    "A": "ĀÁǍÀ", "E": "ĒÉĚÈ", "I": "ĪÍǏÌ", "O": "ŌÓǑÒ", "U": "ŪÚǓÙ", "Ü": "ǕǗǙǛ",
}


def _syllable_to_marks(syl: str) -> str:
    m = re.fullmatch(r"([A-Za-zü:]+)([1-5])", syl)
    if not m:
        return syl.replace("u:", "ü").replace("U:", "Ü")
    body, tone = m.group(1).replace("u:", "ü").replace("U:", "Ü"), int(m.group(2))
    if tone == 5:
        return body
    low = body.lower()
    # Rules: a or e takes the mark; in "ou" the o does; otherwise the last vowel.
    if "a" in low:
        i = low.index("a")
    elif "e" in low:
        i = low.index("e")
    elif "ou" in low:
        i = low.index("o")
    else:
        i = max((low.rfind(v) for v in "iouü"), default=-1)
        if i < 0:  # e.g. "m2", "ng2", "r5"
            return body
    return body[:i] + _TONE_MARKS[body[i]][tone - 1] + body[i + 1:]


def pinyin_marks(numbered: str) -> str:
    """'zhu4 ji1' → 'zhù jī', 'lu:4' → 'lǜ', 'xx5' stays as is."""
    return " ".join(_syllable_to_marks(s) for s in numbered.split())


# ------------------------------------------------------------------ dictionary
@dataclass(frozen=True)
class DictEntry:
    traditional: str
    simplified: str
    pinyin: str  # with tone marks
    definitions: tuple[str, ...]

    @property
    def is_name(self) -> bool:
        return bool(self.pinyin[:1].isupper())

    def short_gloss(self, limit: int = 70) -> str:
        defs = [d for d in self.definitions if not d.startswith(("CL:", "variant of", "old variant of"))] or list(self.definitions)
        text = "; ".join(defs[:3])
        return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def parse_cedict(lines: Iterable[str]) -> list[DictEntry]:
    out = []
    for line in lines:
        if not line or line.startswith("#"):
            continue
        m = _LINE_RX.match(line.rstrip("\n"))
        if not m:
            continue
        trad, simp, py, defs = m.groups()
        out.append(DictEntry(trad, simp, pinyin_marks(py), tuple(d for d in defs.split("/") if d)))
    return out


_VARIANT_PREFIXES = ("variant of", "old variant of", "unofficial variant of", "archaic variant of", "used in ")


def _entry_rank(e: DictEntry) -> tuple:
    first = e.definitions[0] if e.definitions else ""
    variant = len(e.definitions) == 1 and first.startswith(_VARIANT_PREFIXES)
    particle = any("particle" in d or "marker" in d for d in e.definitions[:2])
    return (e.is_name, variant, not particle, -len(e.definitions))


@dataclass
class WordMatch:
    word: str  # as written in the text
    start: int
    end: int
    entries: list[DictEntry]


@dataclass
class LookupResult:
    """Everything about the word under the mouse. Positions are relative to the paragraph text."""

    primary: WordMatch
    shorter: list[WordMatch] = field(default_factory=list)  # other words starting at the hovered character
    container: WordMatch | None = None  # a longer word starting earlier that covers the hovered character
    chars: list[WordMatch] = field(default_factory=list)  # per-character breakdown of a multi-character primary
    sentence: str = ""

    @property
    def word(self) -> str:
        return self.primary.word


class Dictionary:
    def __init__(self, entries: Iterable[DictEntry] = ()):
        self.index: dict[str, list[DictEntry]] = {}
        self.max_len = 1
        for e in entries:
            self._add(e.simplified, e)
            if e.traditional != e.simplified:
                self._add(e.traditional, e)

    def _add(self, key: str, e: DictEntry) -> None:
        bucket = self.index.setdefault(key, [])
        if e not in bucket:
            bucket.append(e)
        self.max_len = max(self.max_len, len(key))

    def __len__(self) -> int:
        return len(self.index)

    @classmethod
    def load(cls, path: Path) -> "Dictionary":
        with open(path, encoding="utf-8") as f:
            return cls(parse_cedict(f))

    def lookup(self, word: str) -> list[DictEntry]:
        """Entries for a word, most useful first.

        CC-CEDICT's order is arbitrary (筑 lists "five-string lute" before 築 "to build"), so rank: grammar particles
        (了 le, 着 zhe), then senses with more definitions (a rough proxy for common use); variants and names last.
        """
        return sorted(self.index.get(word, []), key=_entry_rank)

    def guess_pinyin(self, word: str) -> str:
        """Pinyin for a word that isn't in the dictionary (e.g. a character name), character by character."""
        out = []
        for c in word:
            entries = self.lookup(c)
            out.append(entries[0].pinyin.lower() if entries else c)
        return " ".join(out)

    def words_at(self, text: str, pos: int) -> list[WordMatch]:
        """Dictionary words starting at text[pos], longest first."""
        out = []
        for n in range(min(self.max_len, len(text) - pos), 0, -1):
            w = text[pos:pos + n]
            if w in self.index:
                out.append(WordMatch(w, pos, pos + n, self.lookup(w)))
        return out

    def look_up_at(self, text: str, pos: int, max_shorter: int = 2) -> LookupResult | None:
        if not (0 <= pos < len(text)) or not is_cjk(text[pos]):
            return None
        matches = self.words_at(text, pos)
        if not matches:
            return LookupResult(WordMatch(text[pos], pos, pos + 1, []), sentence=sentence_at(text, pos))
        primary = matches[0]
        res = LookupResult(primary, matches[1:1 + max_shorter], sentence=sentence_at(text, pos))
        # a longer word that starts before pos and still covers it (hovering 基 in 筑基 → 筑基)
        for start in range(max(0, pos - self.max_len + 1), pos):
            for m in self.words_at(text, start):
                if m.end > pos and len(m.word) > len(primary.word) and (res.container is None or len(m.word) > len(res.container.word)):
                    res.container = m
        if len(primary.word) > 1:
            res.chars = [WordMatch(c, primary.start + i, primary.start + i + 1, self.lookup(c)) for i, c in enumerate(primary.word)]
        return res


def is_cjk(ch: str) -> bool:
    o = ord(ch)
    return 0x3400 <= o <= 0x4DBF or 0x4E00 <= o <= 0x9FFF or 0xF900 <= o <= 0xFAFF or 0x20000 <= o <= 0x2FA1F


def sentence_at(text: str, pos: int, limit: int = 160) -> str:
    """The sentence around pos (ends at 。！？… and keeps a closing quote), trimmed to `limit` characters."""
    start = pos
    while start > 0 and text[start - 1] not in _SENTENCE_END:
        start -= 1
    while start < pos and text[start] in _CLOSERS:  # the previous sentence's closing quote
        start += 1
    end = pos
    while end < len(text) and text[end] not in _SENTENCE_END:
        end += 1
    while end < len(text) and (text[end] in _SENTENCE_END or text[end] in _CLOSERS) and text[end] != "\n":
        end += 1
    s = text[start:end].strip()
    if len(s) > limit:  # keep the hovered position inside the window
        rel = pos - start
        lo = max(0, min(rel - limit // 2, len(s) - limit))
        s = ("…" if lo else "") + s[lo:lo + limit] + ("…" if lo + limit < len(s) else "")
    return s


def download_cedict(dest: Path, progress: Callable[[str], None] = lambda _m: None, timeout: int = 60) -> int:
    """Download and unpack CC-CEDICT to `dest`. Returns the number of entries."""
    progress("Dictionary: downloading CC-CEDICT…")
    req = urllib.request.Request(CEDICT_URL, headers={"User-Agent": "LN-Translator"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    text = gzip.decompress(data).decode("utf-8")
    n = len(parse_cedict(text.splitlines()))
    if n < 1000:
        raise RuntimeError("The downloaded dictionary looks wrong (too few entries).")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(dest)
    return n


# ------------------------------------------------------------------ word list
def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


@dataclass
class SavedWord:
    word: str
    pinyin: str = ""
    gloss: str = ""
    lookups: int = 0
    saved: bool = False  # starred by the user
    sentence: str = ""  # where it was last met
    source: str = ""  # novel · chapter
    first_seen: str = ""
    last_seen: str = ""
    explanation: dict | None = None  # cached Claude explanation

    @classmethod
    def from_dict(cls, d: dict) -> "SavedWord":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


class WordList:
    """Every word looked up (hover counts once per chapter visit), stars and cached explanations. One per user."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.words: dict[str, SavedWord] = {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            for d in data.get("words", []):
                w = SavedWord.from_dict(d)
                self.words[w.word] = w
        except (FileNotFoundError, json.JSONDecodeError):
            pass

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        data = {"version": 1, "words": [asdict(w) for w in self.words.values()]}
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def get(self, word: str) -> SavedWord | None:
        return self.words.get(word)

    def _touch(self, word: str, pinyin: str, gloss: str, sentence: str, source: str) -> SavedWord:
        w = self.words.get(word)
        if w is None:
            w = self.words[word] = SavedWord(word, first_seen=_now())
        if pinyin and not w.pinyin:
            w.pinyin = pinyin
        if gloss and not w.gloss:
            w.gloss = gloss
        if sentence:
            w.sentence = sentence
            w.source = source
        w.last_seen = _now()
        return w

    def record_lookup(self, word: str, pinyin: str = "", gloss: str = "", sentence: str = "", source: str = "") -> SavedWord:
        w = self._touch(word, pinyin, gloss, sentence, source)
        w.lookups += 1
        return w

    def set_saved(self, word: str, saved: bool, pinyin: str = "", gloss: str = "", sentence: str = "", source: str = "") -> SavedWord:
        w = self._touch(word, pinyin, gloss, sentence, source)
        w.saved = saved
        return w

    def set_explanation(self, word: str, explanation: dict, sentence: str = "", source: str = "") -> SavedWord:
        w = self._touch(word, explanation.get("pinyin", ""), explanation.get("meaning", ""), sentence, source)
        w.explanation = dict(explanation, context=sentence)
        if explanation.get("meaning"):
            w.gloss = explanation["meaning"]
        return w

    def remove(self, word: str) -> None:
        self.words.pop(word, None)

    def sorted(self, saved_only: bool = False) -> list[SavedWord]:
        items = [w for w in self.words.values() if w.saved or not saved_only]
        items.sort(key=lambda w: w.last_seen, reverse=True)  # most recent first among equals
        items.sort(key=lambda w: (not w.saved, -w.lookups))
        return items
