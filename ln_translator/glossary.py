"""Glossary storage helpers, term matching, consistency checks and Sync diffing."""

from __future__ import annotations

import csv
import io
import json
import re

from .models import GlossaryEntry, ProposedChange, Warning
from .text_utils import split_paragraphs

CSV_FIELDS = ["zh", "en", "category", "aliases", "gender", "notes", "confirmed", "first_seen"]


class Glossary:
    def __init__(self, entries: list[GlossaryEntry] | None = None):
        self.entries: list[GlossaryEntry] = entries or []
        self._matcher: re.Pattern | None = None
        self._key_map: dict[str, GlossaryEntry] = {}

    # ----- basic operations -------------------------------------------------
    def invalidate(self) -> None:
        self._matcher = None

    def add(self, entry: GlossaryEntry) -> None:
        self.entries.append(entry)
        self.invalidate()

    def remove(self, entry_id: str) -> None:
        self.entries = [e for e in self.entries if e.id != entry_id]
        self.invalidate()

    def get(self, entry_id: str) -> GlossaryEntry | None:
        return next((e for e in self.entries if e.id == entry_id), None)

    def find_by_key(self, zh: str) -> GlossaryEntry | None:
        zh = zh.strip()
        for e in self.entries:
            if zh in e.keys():
                return e
        return None

    # ----- matching ---------------------------------------------------------
    def _build(self) -> None:
        self._key_map = {}
        # Longest keys win: when one key contains another (王林 vs 王), prefer the longer.
        for e in sorted(self.entries, key=lambda e: e.confirmed, reverse=True):
            for k in e.keys():
                self._key_map.setdefault(k, e)
        keys = sorted(self._key_map, key=len, reverse=True)
        self._matcher = re.compile("|".join(re.escape(k) for k in keys)) if keys else None

    def matches(self, text: str) -> list[tuple[int, int, GlossaryEntry]]:
        """Non-overlapping (start, end, entry) matches, preferring longer terms."""
        if self._matcher is None:
            self._build()
        if self._matcher is None:
            return []
        return [(m.start(), m.end(), self._key_map[m.group(0)]) for m in self._matcher.finditer(text)]

    def relevant_entries(self, text: str) -> list[GlossaryEntry]:
        seen: dict[str, GlossaryEntry] = {}
        for _, _, e in self.matches(text):
            seen.setdefault(e.id, e)
        return list(seen.values())

    # ----- serialisation ----------------------------------------------------
    def to_json(self) -> str:
        return json.dumps([e.to_dict() for e in self.entries], ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, text: str) -> "Glossary":
        data = json.loads(text) if text.strip() else []
        if isinstance(data, dict):
            data = data.get("entries", [])
        return cls([GlossaryEntry.from_dict(d) for d in data if d.get("zh")])

    def to_csv(self) -> str:
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=CSV_FIELDS)
        w.writeheader()
        for e in self.entries:
            d = e.to_dict()
            d["aliases"] = "; ".join(e.aliases)
            d["confirmed"] = "yes" if e.confirmed else ""
            w.writerow({k: d.get(k, "") for k in CSV_FIELDS})
        return buf.getvalue()

    @staticmethod
    def parse_csv(text: str) -> list[GlossaryEntry]:
        reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
        out = []
        for row in reader:
            row = {(k or "").strip().lower(): v for k, v in row.items()}
            if row.get("zh"):
                out.append(GlossaryEntry.from_dict(row))
        return out

    def merge(self, incoming: list[GlossaryEntry], overwrite: bool) -> tuple[int, int]:
        """Merge imported entries. Returns (added, updated)."""
        added = updated = 0
        for new in incoming:
            existing = self.find_by_key(new.zh)
            if existing is None:
                self.entries.append(new)
                added += 1
            elif overwrite:
                new.id = existing.id
                self.entries[self.entries.index(existing)] = new
                updated += 1
        self.invalidate()
        return added, updated


def format_for_prompt(entries: list[GlossaryEntry]) -> str:
    lines = []
    for e in sorted(entries, key=lambda e: (e.category, e.zh)):
        head = e.zh
        if e.aliases:
            head += f" (also: {', '.join(e.aliases)})"
        meta = [e.category]
        if e.gender:
            meta.append(e.gender)
        line = f"{head} = {e.en} [{'; '.join(meta)}]"
        if e.notes:
            line += f" — {e.notes}"
        lines.append(line)
    return "\n".join(lines)


def _contains_en(haystack: str, needle: str) -> bool:
    needle = needle.strip()
    if not needle:
        return True
    return needle.lower() in haystack.lower()


def check_consistency(glossary: Glossary, raw: str, translation: str) -> list[Warning]:
    """Deterministic check that glossary terms present in the raw text appear in the translation.

    Works per paragraph when the two texts are aligned, otherwise over the whole chapter.
    """
    raw_paras, en_paras = split_paragraphs(raw), split_paragraphs(translation)
    if not raw_paras or not en_paras:
        return []
    warnings: list[Warning] = []
    if len(raw_paras) == len(en_paras):
        for i, (zh, en) in enumerate(zip(raw_paras, en_paras)):
            for e in glossary.relevant_entries(zh):
                if not _contains_en(en, e.en):
                    warnings.append(
                        Warning(
                            kind="missing",
                            zh=e.zh,
                            suggested_en=e.en,
                            category=e.category,
                            reason=f"Glossary term not found in paragraph {i + 1}",
                            paragraph=i,
                            entry_id=e.id,
                        )
                    )
    else:
        full_en = "\n".join(en_paras)
        for e in glossary.relevant_entries("\n".join(raw_paras)):
            if not _contains_en(full_en, e.en):
                warnings.append(
                    Warning(
                        kind="missing",
                        zh=e.zh,
                        suggested_en=e.en,
                        category=e.category,
                        reason="Glossary term not found anywhere in the translation",
                        entry_id=e.id,
                    )
                )
    return warnings


def diff_proposals(glossary: Glossary, proposals: list[GlossaryEntry]) -> list[ProposedChange]:
    """Compare AI-extracted terms against the glossary for the Sync review screen."""
    changes: list[ProposedChange] = []
    seen: set[str] = set()
    for p in proposals:
        if not p.zh or not p.en or p.zh in seen:
            continue
        seen.add(p.zh)
        existing = glossary.find_by_key(p.zh)
        if existing is None:
            changes.append(ProposedChange("add", p, None, "New term"))
            continue
        if existing.en.strip().lower() != p.en.strip().lower():
            changes.append(
                ProposedChange(
                    "conflict",
                    p,
                    existing,
                    f"Glossary has “{existing.en}”" + (" (confirmed)" if existing.confirmed else ""),
                    accept=not existing.confirmed,
                )
            )
            continue
        new_aliases = [a for a in p.aliases if a not in existing.keys()]
        extra = []
        if new_aliases:
            extra.append("aliases: " + ", ".join(new_aliases))
        if p.gender and not existing.gender:
            extra.append("gender: " + p.gender)
        if p.notes and not existing.notes:
            extra.append("notes")
        if extra:
            changes.append(ProposedChange("update", p, existing, "Adds " + "; ".join(extra)))
    order = {"conflict": 0, "add": 1, "update": 2}
    changes.sort(key=lambda c: (order[c.action], c.proposal.category, c.proposal.zh))
    return changes


def apply_changes(glossary: Glossary, changes: list[ProposedChange], chapter_id: str) -> int:
    applied = 0
    for c in changes:
        if not c.accept:
            continue
        p = c.proposal
        if c.action == "add":
            p.first_seen = p.first_seen or chapter_id
            glossary.entries.append(p)
        elif c.existing is not None:
            e = c.existing
            if c.action == "conflict":
                e.en = p.en
                if p.category != "other":
                    e.category = p.category
            for a in p.aliases:
                if a not in e.keys():
                    e.aliases.append(a)
            e.gender = e.gender or p.gender
            e.notes = e.notes or p.notes
        applied += 1
    glossary.invalidate()
    return applied
