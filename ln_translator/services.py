"""High-level AI operations: Translate, Retranslate, Sync and Summary. No UI code here."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Callable

from . import prompts
from .claude_bridge import CallStats, ClaudeBridge, ProgressFn
from .glossary import Glossary, format_for_prompt
from .models import CATEGORIES, GlossaryEntry, Warning
from .text_utils import chunk_indices, split_paragraphs

UNTRANSLATED_MARK = "⟦untranslated⟧ "


@dataclass
class TotalStats:
    calls: int = 0
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    five_hour_utilization: float | None = None
    seven_day_utilization: float | None = None

    def add(self, s: CallStats) -> None:
        self.calls += 1
        self.cost_usd += s.cost_usd
        self.input_tokens += s.input_tokens
        self.output_tokens += s.output_tokens
        if s.five_hour_utilization is not None:
            self.five_hour_utilization = s.five_hour_utilization
        if s.seven_day_utilization is not None:
            self.seven_day_utilization = s.seven_day_utilization


@dataclass
class TranslateOutcome:
    paragraphs: dict[int, str]
    warnings: list[Warning] = field(default_factory=list)
    stats: TotalStats = field(default_factory=TotalStats)


@dataclass
class SyncOutcome:
    terms: list[GlossaryEntry]
    style_observations: list[str]
    stats: TotalStats = field(default_factory=TotalStats)


class Translator:
    def __init__(self, bridge: ClaudeBridge, chunk_chars: int = 3000, context_paragraphs: int = 3, summary_effort: str = "low",
                 naming: dict[str, str] | None = None):
        self.bridge = bridge
        self.naming = naming or {}  # category -> "meaning" | "pinyin" | "hybrid" | "auto"
        self.chunk_chars = chunk_chars
        self.context_paragraphs = context_paragraphs
        self.summary_effort = summary_effort

    # ------------------------------------------------------------------ translate
    def _translate_call(
        self,
        targets: dict[int, str],
        glossary: Glossary,
        style: str,
        summary: str,
        preceding: list[str],
        context_after: dict[int, str] | None,
        instruction: str,
        progress: ProgressFn,
        cancel: threading.Event,
        stats: TotalStats,
        label: str,
    ) -> tuple[dict[int, str], list[Warning]]:
        glossary_text = format_for_prompt(glossary.relevant_entries("\n".join(targets.values())))
        prompt = prompts.build_translate_prompt(
            targets, glossary_text, style, summary, preceding, context_after, instruction, self.naming
        )
        res = self.bridge.run(prompt, prompts.TRANSLATE_SYSTEM, prompts.TRANSLATE_SCHEMA, progress, cancel, label=label)
        stats.add(res.stats)
        out: dict[int, str] = {}
        for item in res.data.get("paragraphs", []):
            try:
                idx = int(item.get("index"))
            except (TypeError, ValueError):
                continue
            text = str(item.get("en", "")).strip().replace("\n", " ")
            if idx in targets and text:
                out[idx] = text
        warnings = []
        for w in res.data.get("warnings", []):
            warn = Warning.from_dict({**w, "kind": "uncertain"})
            if warn.category not in CATEGORIES:
                warn.category = "other"
            if warn.paragraph is not None and warn.paragraph not in targets:
                warn.paragraph = min(targets, key=lambda i: abs(i - warn.paragraph))
            if warn.zh and not glossary.find_by_key(warn.zh):
                warnings.append(warn)
        return out, warnings

    def _translate_indices(
        self,
        raw_paras: list[str],
        en_paras: dict[int, str],
        indices: list[int],
        glossary: Glossary,
        style: str,
        summary: str,
        instruction: str,
        progress: ProgressFn,
        cancel: threading.Event,
        on_chunk: Callable[[dict[int, str]], None] | None,
        stats: TotalStats,
        provisional: list[Warning] | None = None,
    ) -> TranslateOutcome:
        subset = [raw_paras[i] for i in indices]
        chunks = [[indices[j] for j in c] for c in chunk_indices(subset, self.chunk_chars)]
        result: dict[int, str] = {}
        warnings: list[Warning] = []
        # Work on a copy so provisional choices (unconfirmed warnings) keep later chunks consistent
        # without touching the user's glossary.
        glossary = Glossary(list(glossary.entries))
        provisional = list(provisional or [])

        def add_provisional(ws: list[Warning]) -> None:
            for w in ws:
                if w.kind == "uncertain" and w.zh and w.suggested_en and not glossary.find_by_key(w.zh):
                    glossary.add(GlossaryEntry(zh=w.zh, en=w.suggested_en, category=w.category,
                                               notes="provisional: chosen earlier in this chapter, not yet confirmed"))

        add_provisional(provisional)
        for n, chunk in enumerate(chunks, 1):
            if cancel.is_set():
                break
            label = f"Chunk {n}/{len(chunks)}"
            targets = {i: raw_paras[i] for i in chunk}
            first = chunk[0]
            known = {**en_paras, **result}
            preceding = [known[i] for i in range(max(0, first - self.context_paragraphs), first) if i in known]
            last = chunk[-1]
            after = {i: raw_paras[i] for i in range(last + 1, min(len(raw_paras), last + 3)) if i not in targets}
            got, warns = self._translate_call(
                targets, glossary, style, summary, preceding, after, instruction, progress, cancel, stats, label
            )
            missing = {i: t for i, t in targets.items() if i not in got}
            if missing and not cancel.is_set():
                progress(f"{label}: retrying {len(missing)} missing paragraph(s)…")
                got2, warns2 = self._translate_call(
                    missing, glossary, style, summary, preceding, after, instruction, progress, cancel, stats, label + " retry"
                )
                got.update(got2)
                warns += warns2
            for i, t in targets.items():
                if i not in got:
                    got[i] = UNTRANSLATED_MARK + t
                    warnings.append(
                        Warning(kind="uncertain", zh=t[:20], reason="Claude did not return this paragraph", paragraph=i)
                    )
            result.update(got)
            warnings += warns
            add_provisional(warns)
            if on_chunk:
                on_chunk(dict(result))
        return TranslateOutcome(result, _dedupe(warnings), stats)

    def translate_chapter(
        self,
        raw: str,
        glossary: Glossary,
        style: str,
        summary: str,
        progress: ProgressFn,
        cancel: threading.Event,
        on_chunk: Callable[[dict[int, str]], None] | None = None,
    ) -> TranslateOutcome:
        raw_paras = split_paragraphs(raw)
        if not raw_paras:
            raise ValueError("Frame A is empty — paste a Chinese chapter first.")
        return self._translate_indices(
            raw_paras, {}, list(range(len(raw_paras))), glossary, style, summary, "", progress, cancel, on_chunk, TotalStats()
        )

    def retranslate(
        self,
        raw: str,
        translation: str,
        indices: list[int],
        glossary: Glossary,
        style: str,
        summary: str,
        instruction: str,
        progress: ProgressFn,
        cancel: threading.Event,
        provisional: list[Warning] | None = None,
    ) -> TranslateOutcome:
        """Retranslate some paragraphs. `provisional` = the chapter's pending uncertain-term warnings,
        so terms Claude chose earlier are reused instead of being translated differently."""
        raw_paras = split_paragraphs(raw)
        en_list = split_paragraphs(translation)
        if len(raw_paras) != len(en_list):
            raise ValueError(
                "Frames A and B have a different number of paragraphs, so paragraphs can't be matched. "
                "Translate the whole chapter instead."
            )
        en_paras = {i: t for i, t in enumerate(en_list) if i not in indices}
        indices = sorted(i for i in set(indices) if 0 <= i < len(raw_paras))
        if not indices:
            raise ValueError("No paragraphs selected.")
        return self._translate_indices(
            raw_paras, en_paras, indices, glossary, style, summary, instruction, progress, cancel, None, TotalStats(),
            provisional,
        )

    # ------------------------------------------------------------------ sync
    def sync(self, raw: str, translation: str, glossary: Glossary, progress: ProgressFn, cancel: threading.Event) -> SyncOutcome:
        if not raw.strip() or not translation.strip():
            raise ValueError("Sync needs a Chinese chapter in Frame A and its English translation in Frame B.")
        stats = TotalStats()
        glossary_text = format_for_prompt(glossary.relevant_entries(raw))
        prompt = prompts.build_sync_prompt(raw, translation, glossary_text)
        res = self.bridge.run(prompt, prompts.SYNC_SYSTEM, prompts.SYNC_SCHEMA, progress, cancel, label="Sync")
        stats.add(res.stats)
        terms = []
        for t in res.data.get("terms", []):
            entry = GlossaryEntry.from_dict(t)
            # Only keep terms that actually occur in the chapter (guards against invented terms).
            if entry.zh and entry.en and any(k in raw for k in entry.keys()):
                entry.aliases = [a for a in entry.aliases if a != entry.zh]
                terms.append(entry)
        obs = [str(o).strip() for o in res.data.get("style_observations", []) if str(o).strip()]
        return SyncOutcome(terms, obs, stats)

    # ------------------------------------------------------------------ summary
    def update_summary(
        self, old_summary: str, chapter_label: str, translation: str, progress: ProgressFn, cancel: threading.Event
    ) -> tuple[str, TotalStats]:
        stats = TotalStats()
        prompt = prompts.build_summary_prompt(old_summary, chapter_label, translation)
        res = self.bridge.run(
            prompt, prompts.SUMMARY_SYSTEM, prompts.SUMMARY_SCHEMA, progress, cancel, effort=self.summary_effort, label="Summary"
        )
        stats.add(res.stats)
        return str(res.data.get("summary", "")).strip() or old_summary, stats

    def ping(self, progress: ProgressFn, cancel: threading.Event) -> TotalStats:
        stats = TotalStats()
        res = self.bridge.run('Return {"ok": true}.', prompts.PING_SYSTEM, prompts.PING_SCHEMA, progress, cancel, effort="low", label="Test")
        stats.add(res.stats)
        if not res.data.get("ok"):
            raise RuntimeError("Unexpected reply from Claude Code.")
        return stats


def _dedupe(warnings: list[Warning]) -> list[Warning]:
    seen, out = set(), []
    for w in warnings:
        key = (w.kind, w.zh, w.paragraph if w.kind == "missing" else None)
        if key in seen:
            continue
        seen.add(key)
        out.append(w)
    return out


def explain_word(
    bridge: ClaudeBridge,
    word: str,
    sentence: str,
    progress: ProgressFn,
    cancel: threading.Event,
    novel: str = "",
    dictionary_hint: str = "",
    glossary_hint: str = "",
    effort: str = "low",
) -> tuple[dict, TotalStats]:
    """Ask Claude what a word means in its sentence (reading help). Returns the EXPLAIN_SCHEMA dict."""
    stats = TotalStats()
    prompt = prompts.build_explain_prompt(word, sentence, novel, dictionary_hint, glossary_hint)
    res = bridge.run(prompt, prompts.EXPLAIN_SYSTEM, prompts.EXPLAIN_SCHEMA, progress, cancel, effort=effort, label="Explain")
    stats.add(res.stats)
    data = dict(res.data)
    data["examples"] = [e for e in data.get("examples") or [] if isinstance(e, dict) and e.get("zh")][:2]
    return data, stats
