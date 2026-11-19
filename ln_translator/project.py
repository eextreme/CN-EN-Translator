"""A novel project is a plain folder on disk:

    MyNovel/
      project.json
      glossary.json
      style.md            per-novel style guide (sent with every translation)
      summary.md          rolling "story so far" (sent with every translation)
      chapters/<id>/raw.txt, translation.txt, meta.json
      history/chapters/<id>/<timestamp>_<label>.txt
      history/glossary/<timestamp>_<label>.json
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .glossary import Glossary
from .models import DEFAULT_NAMING, NAMING_MODES, Warning

DEFAULT_STYLE = """# Style guide
(How unique names are rendered — meaning vs. pinyin — is set per category in the panel on the right.)
- Person names: surname first (林动 → Lin Dong).
- Honorifics: translate relationship terms (师兄 → Senior Brother, 师父 → Master).
- Idioms (成语): render the meaning naturally; don't translate literally.
- Tone: natural modern English, past tense, keep the author's humour.
- Units: keep traditional units (li, zhang) and do not convert.
"""

_SAFE = re.compile(r"[^\w\-]+")


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _read(path: Path, default: str = "") -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return default


@dataclass
class Chapter:
    id: str
    title: str = ""
    raw: str = ""
    translation: str = ""
    warnings: list[Warning] = field(default_factory=list)
    source: str = ""  # "reference" | "ai" | "edited" | ""

    @property
    def label(self) -> str:
        return f"{self.id} — {self.title}" if self.title else self.id


@dataclass
class HistoryItem:
    path: Path
    timestamp: str
    label: str


class Project:
    def __init__(self, root: Path):
        self.root = Path(root)
        self._meta = json.loads(_read(self.root / "project.json", "{}") or "{}")
        self.name: str = self._meta.get("name") or self.root.name
        self.glossary = Glossary.from_json(_read(self.root / "glossary.json", "[]"))

    # ----- naming policy (meaning vs. pinyin per category) --------------------
    @property
    def naming(self) -> dict[str, str]:
        saved = self._meta.get("naming") or {}
        return {c: saved.get(c, DEFAULT_NAMING[c]) if saved.get(c, "") in NAMING_MODES else DEFAULT_NAMING[c]
                for c in DEFAULT_NAMING}

    @naming.setter
    def naming(self, value: dict[str, str]) -> None:
        self._meta["naming"] = {c: m for c, m in value.items() if c in DEFAULT_NAMING and m in NAMING_MODES}
        _write(self.root / "project.json", json.dumps(self._meta, ensure_ascii=False, indent=2))

    # ----- creation ---------------------------------------------------------
    @classmethod
    def create(cls, root: Path, name: str) -> "Project":
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        if not (root / "project.json").exists():
            _write(root / "project.json", json.dumps({"name": name, "format": 1, "created": _stamp()}, indent=2))
        if not (root / "glossary.json").exists():
            _write(root / "glossary.json", "[]")
        if not (root / "style.md").exists():
            _write(root / "style.md", DEFAULT_STYLE)
        if not (root / "summary.md").exists():
            _write(root / "summary.md", "")
        (root / "chapters").mkdir(exist_ok=True)
        return cls(root)

    @staticmethod
    def is_project(root: Path) -> bool:
        return (Path(root) / "project.json").exists()

    # ----- glossary, style, summary ----------------------------------------
    def save_glossary(self) -> None:
        _write(self.root / "glossary.json", self.glossary.to_json())

    def snapshot_glossary(self, label: str) -> None:
        _write(self.root / "history" / "glossary" / f"{_stamp()}_{_SAFE.sub('-', label)}.json", self.glossary.to_json())

    def restore_glossary(self, path: Path) -> None:
        self.snapshot_glossary("before-restore")
        self.glossary = Glossary.from_json(_read(path, "[]"))
        self.save_glossary()

    @property
    def style(self) -> str:
        return _read(self.root / "style.md")

    @style.setter
    def style(self, text: str) -> None:
        _write(self.root / "style.md", text)

    @property
    def summary(self) -> str:
        return _read(self.root / "summary.md")

    @summary.setter
    def summary(self, text: str) -> None:
        _write(self.root / "summary.md", text)

    # ----- chapters ---------------------------------------------------------
    def _chapter_dir(self, cid: str) -> Path:
        return self.root / "chapters" / cid

    def chapter_ids(self) -> list[str]:
        d = self.root / "chapters"
        if not d.exists():
            return []
        return sorted(p.name for p in d.iterdir() if p.is_dir())

    def next_chapter_id(self) -> str:
        nums = [int(c) for c in self.chapter_ids() if c.isdigit()]
        return f"{(max(nums) + 1) if nums else 1:04d}"

    def load_chapter(self, cid: str) -> Chapter:
        d = self._chapter_dir(cid)
        meta = json.loads(_read(d / "meta.json", "{}") or "{}")
        return Chapter(
            id=cid,
            title=meta.get("title", ""),
            raw=_read(d / "raw.txt"),
            translation=_read(d / "translation.txt"),
            warnings=[Warning.from_dict(w) for w in meta.get("warnings", [])],
            source=meta.get("source", ""),
        )

    def save_chapter(self, ch: Chapter, history_label: str | None = None) -> None:
        """Save a chapter. If history_label is given and the translation changed, keep the old version."""
        d = self._chapter_dir(ch.id)
        old = _read(d / "translation.txt")
        if history_label and old.strip() and old != ch.translation:
            self.add_history(ch.id, old, history_label)
        _write(d / "raw.txt", ch.raw)
        _write(d / "translation.txt", ch.translation)
        meta = {
            "title": ch.title,
            "source": ch.source,
            "warnings": [w.to_dict() for w in ch.warnings],
            "updated": _stamp(),
        }
        _write(d / "meta.json", json.dumps(meta, ensure_ascii=False, indent=2))

    def new_chapter(self, title: str = "", cid: str | None = None) -> Chapter:
        cid = _SAFE.sub("-", cid).strip("-") if cid else self.next_chapter_id()
        if not cid or self._chapter_dir(cid).exists():
            raise ValueError(f"Chapter '{cid}' already exists or is invalid")
        ch = Chapter(id=cid, title=title)
        self.save_chapter(ch)
        return ch

    def rename_chapter(self, cid: str, new_id: str) -> str:
        new_id = _SAFE.sub("-", new_id).strip("-")
        if not new_id or self._chapter_dir(new_id).exists():
            raise ValueError(f"Chapter '{new_id}' already exists or is invalid")
        self._chapter_dir(cid).rename(self._chapter_dir(new_id))
        hist = self.root / "history" / "chapters" / cid
        if hist.exists():
            hist.rename(self.root / "history" / "chapters" / new_id)
        return new_id

    def delete_chapter(self, cid: str) -> None:
        d = self._chapter_dir(cid)
        if d.exists():
            # Keep a copy in history rather than destroying the user's work outright.
            trash = self.root / "history" / "deleted" / f"{cid}_{_stamp()}"
            trash.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(d), str(trash))

    # ----- history ----------------------------------------------------------
    def add_history(self, cid: str, text: str, label: str) -> None:
        _write(self.root / "history" / "chapters" / cid / f"{_stamp()}_{_SAFE.sub('-', label)}.txt", text)

    def _list_history(self, folder: Path, suffix: str) -> list[HistoryItem]:
        if not folder.exists():
            return []
        items = []
        for p in sorted(folder.glob(f"*{suffix}"), reverse=True):
            stem = p.stem
            ts, _, label = stem.partition("_")
            try:
                when = datetime.strptime(ts, "%Y%m%d-%H%M%S-%f").strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                when = ts
            items.append(HistoryItem(p, when, label))
        return items

    def chapter_history(self, cid: str) -> list[HistoryItem]:
        return self._list_history(self.root / "history" / "chapters" / cid, ".txt")

    def glossary_history(self) -> list[HistoryItem]:
        return self._list_history(self.root / "history" / "glossary", ".json")

    @staticmethod
    def read_history(item: HistoryItem) -> str:
        return _read(item.path)

    # ----- export -----------------------------------------------------------
    def export_translations(self, path: Path) -> int:
        parts = []
        for cid in self.chapter_ids():
            ch = self.load_chapter(cid)
            if ch.translation.strip():
                parts.append(f"{ch.label}\n\n{ch.translation.strip()}\n")
        Path(path).write_text("\n\n".join(parts), encoding="utf-8")
        return len(parts)
