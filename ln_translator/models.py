"""Plain data types shared by the core logic and the UI."""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field

CATEGORIES = [
    "character",
    "place",
    "organization",
    "cultivation",
    "technique",
    "item",
    "title",
    "creature",
    "concept",
    "other",
]

GENDERS = ["", "male", "female", "neutral", "unknown"]

# How to render unique names/terms that are not yet in the glossary.
NAMING_MODES = {
    "meaning": ("Meaning (direct)", "translate by meaning, e.g. 蓝月 → Blue Moon, 青云宗 → Azure Cloud Sect"),
    "pinyin": ("Pinyin (indirect)", "transliterate fully in pinyin, e.g. 南岳 → Nan Yue, 青云宗 → Qingyun Zong"),
    "hybrid": ("Pinyin + translated suffix", "pinyin for the name, translate generic suffixes, e.g. 南岳山 → Nanyue Mountain, 青云宗 → Qingyun Sect"),
    "auto": ("Claude decides", "choose whatever reads best and is most conventional in English translations of this genre"),
}

DEFAULT_NAMING = {
    "character": "pinyin",
    "place": "hybrid",
    "organization": "meaning",
    "cultivation": "meaning",
    "technique": "meaning",
    "item": "meaning",
    "title": "meaning",
    "creature": "meaning",
    "concept": "meaning",
    "other": "auto",
}


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


@dataclass
class GlossaryEntry:
    zh: str
    en: str
    category: str = "other"
    aliases: list[str] = field(default_factory=list)  # other Chinese forms that map to the same English
    gender: str = ""
    notes: str = ""
    confirmed: bool = False  # user-confirmed entries are never overwritten by Sync
    first_seen: str = ""  # chapter id
    id: str = field(default_factory=_new_id)

    def keys(self) -> list[str]:
        """All Chinese strings that identify this entry."""
        return [k for k in [self.zh, *self.aliases] if k]

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "GlossaryEntry":
        aliases = d.get("aliases") or []
        if isinstance(aliases, str):
            aliases = split_aliases(aliases)
        category = str(d.get("category") or "other").strip().lower()
        if category not in CATEGORIES:
            category = "other"
        return cls(
            zh=str(d.get("zh", "")).strip(),
            en=str(d.get("en", "")).strip(),
            category=category,
            aliases=[a.strip() for a in aliases if str(a).strip()],
            gender=str(d.get("gender") or "").strip().lower(),
            notes=str(d.get("notes") or "").strip(),
            confirmed=_to_bool(d.get("confirmed", False)),
            first_seen=str(d.get("first_seen") or "").strip(),
            id=str(d.get("id") or _new_id()),
        )


def split_aliases(text: str) -> list[str]:
    for sep in ["；", ";", "，", ",", "、", "|"]:
        text = text.replace(sep, ",")
    return [a.strip() for a in text.split(",") if a.strip()]


def _to_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in {"1", "true", "yes", "y", "x", "✓"}


@dataclass
class Warning:
    """Something the user should look at: an uncertain AI choice or a consistency problem."""

    kind: str  # "uncertain" (AI flagged) | "missing" (glossary term not used) | "conflict"
    zh: str
    suggested_en: str = ""
    category: str = "other"
    reason: str = ""
    paragraph: int | None = None
    entry_id: str = ""  # glossary entry involved, for "missing"
    alternative_en: str = ""  # the other rendering style (pinyin vs. meaning)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Warning":
        p = d.get("paragraph")
        return cls(
            kind=str(d.get("kind", "uncertain")),
            zh=str(d.get("zh", "")),
            suggested_en=str(d.get("suggested_en", "")),
            category=str(d.get("category") or "other"),
            reason=str(d.get("reason", "")),
            paragraph=int(p) if isinstance(p, (int, float)) or (isinstance(p, str) and p.isdigit()) else None,
            entry_id=str(d.get("entry_id", "")),
            alternative_en=str(d.get("alternative_en", "")),
        )


@dataclass
class ProposedChange:
    """One row of the Sync review screen."""

    action: str  # "add" | "update" | "conflict"
    proposal: GlossaryEntry
    existing: GlossaryEntry | None = None
    detail: str = ""
    accept: bool = True
