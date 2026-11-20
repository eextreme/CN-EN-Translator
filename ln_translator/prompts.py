"""System prompts, prompt builders and JSON schemas for every AI operation."""

from __future__ import annotations

from .models import CATEGORIES, NAMING_MODES


def format_naming(naming: dict[str, str]) -> str:
    lines = [f"- {cat}: {NAMING_MODES[mode][1]}" for cat, mode in naming.items() if mode in NAMING_MODES]
    return "## Name rendering for new terms (the glossary overrides this)\n" + "\n".join(lines)

# --------------------------------------------------------------------------- translate

TRANSLATE_SYSTEM = """You are an expert literary translator of Chinese web novels (xianxia, xuanhuan, wuxia, \
urban fantasy, romance and so on) into fluent, natural, publishable English.

Rules:
1. The glossary is authoritative. Every time a glossary term (or one of its aliases) appears, use its English \
translation exactly. Inflect naturally (possessives, plurals) but never substitute a different rendering.
2. Follow the style guide. Use the story summary and preceding paragraphs only to resolve ambiguity \
(who is speaking, pronouns, omitted subjects); do not translate them.
3. Translate every paragraph listed under "Paragraphs to translate". Return exactly one output item per input \
paragraph, using the same index. Never merge, split, skip or reorder paragraphs. Keep each paragraph on one line.
4. Translate completely and faithfully, but write English that reads naturally; do not add translator notes, \
explanations or bracketed pinyin.
5. Warnings: for each story-specific term NOT in the glossary that a reader would need to see translated \
consistently (character names, nicknames, places, sects, cultivation realms, techniques, items, titles, creatures, \
special concepts) report it once in `warnings` with the English you used, its category, and a short reason \
(e.g. "new name", "ambiguous: could be a title or a name", "pun lost in translation"). Also warn when a glossary \
term seems wrong in this context. Do not warn about ordinary vocabulary. Use the translation you chose \
consistently throughout.
6. For character warnings, mention gender in the reason if it is clear from context.
7. Render new unique names and terms (anything not in the glossary) according to the "Name rendering" rules \
for their category: by meaning (蓝月 → Blue Moon), in pinyin (南岳 → Nan Yue), or pinyin with the generic suffix \
translated (南岳山 → Nanyue Mountain). The glossary always overrides these rules. In each warning, give \
`alternative_en`: the same term rendered the other way (the pinyin form if you translated by meaning, the \
meaning-based form if you used pinyin), so the user can choose."""

_CATEGORY_ENUM = {"type": "string", "enum": CATEGORIES}

TRANSLATE_SCHEMA = {
    "type": "object",
    "properties": {
        "paragraphs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"index": {"type": "integer"}, "en": {"type": "string"}},
                "required": ["index", "en"],
                "additionalProperties": False,
            },
        },
        "warnings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "zh": {"type": "string"},
                    "suggested_en": {"type": "string"},
                    "alternative_en": {"type": "string"},
                    "category": _CATEGORY_ENUM,
                    "reason": {"type": "string"},
                    "paragraph": {"type": "integer"},
                },
                "required": ["zh", "suggested_en", "alternative_en", "category", "reason", "paragraph"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["paragraphs", "warnings"],
    "additionalProperties": False,
}


def build_translate_prompt(
    paragraphs: dict[int, str],
    glossary_text: str,
    style: str,
    summary: str,
    preceding: list[str],
    context_after: dict[int, str] | None = None,
    instruction: str = "",
    naming: dict[str, str] | None = None,
) -> str:
    parts = []
    if style.strip():
        parts.append("## Style guide\n" + style.strip())
    if naming:
        parts.append(format_naming(naming))
    if summary.strip():
        parts.append("## Story so far (context only)\n" + summary.strip())
    parts.append("## Glossary (authoritative)\n" + (glossary_text or "(no glossary terms appear in these paragraphs)"))
    if preceding:
        parts.append("## Preceding paragraphs, already translated (context only — do not output)\n" + "\n".join(preceding))
    if instruction.strip():
        parts.append("## Extra instruction from the user\n" + instruction.strip())
    parts.append("## Paragraphs to translate\n" + "\n".join(f"[{i}] {t}" for i, t in sorted(paragraphs.items())))
    if context_after:
        parts.append(
            "## Following paragraphs (context only — do not output)\n"
            + "\n".join(f"[{i}] {t}" for i, t in sorted(context_after.items()))
        )
    parts.append(
        f"Return {len(paragraphs)} translated paragraph(s) with indices "
        f"{', '.join(str(i) for i in sorted(paragraphs))}, plus any warnings."
    )
    return "\n\n".join(parts)


# --------------------------------------------------------------------------- sync

SYNC_SYSTEM = """You build a terminology glossary for a Chinese web novel by comparing a Chinese chapter \
with an existing English translation of the same chapter.

Extract every story-specific term and how THIS translation renders it:
- character names, courtesy names, nicknames and forms of address used as names
- places, sects, clans, organisations, countries
- cultivation realms/levels, techniques, skills, bloodlines, physiques
- artifacts, pills, weapons, treasures, currencies
- titles and ranks, creatures and races, special concepts and invented terms

Rules:
- `en` must be the exact English used by the translation (copy its capitalisation). If the translation \
renders a term inconsistently, use the most frequent rendering and mention the others in `notes`.
- `zh` is the main Chinese form; put other Chinese forms that the translation renders the same way in `aliases`.
- For characters give `gender` (male/female/unknown) when it can be inferred, and a short identifying note \
(role or relationship, e.g. "protagonist's senior brother").
- Do NOT include ordinary vocabulary, common idioms or generic words (e.g. 师父 alone, 天, 剑) unless the \
translation treats them as a fixed proper term.
- The current glossary is provided; still include terms that already exist so differences can be detected, \
but don't invent terms that do not appear in the chapter.
- In `style_observations`, list up to 8 short, concrete observations about how the translator works \
(honorifics, name order, pinyin vs. meaning translation, idioms, units, tone) that would help another \
translator match the style."""

SYNC_SCHEMA = {
    "type": "object",
    "properties": {
        "terms": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "zh": {"type": "string"},
                    "en": {"type": "string"},
                    "category": _CATEGORY_ENUM,
                    "aliases": {"type": "array", "items": {"type": "string"}},
                    "gender": {"type": "string", "enum": ["", "male", "female", "unknown"]},
                    "notes": {"type": "string"},
                },
                "required": ["zh", "en", "category", "aliases", "gender", "notes"],
                "additionalProperties": False,
            },
        },
        "style_observations": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["terms", "style_observations"],
    "additionalProperties": False,
}


def build_sync_prompt(raw: str, translation: str, glossary_text: str) -> str:
    return "\n\n".join(
        [
            "## Current glossary entries that appear in this chapter\n" + (glossary_text or "(none)"),
            "## Chinese chapter\n" + raw.strip(),
            "## English translation of the same chapter\n" + translation.strip(),
            "Extract the terminology now.",
        ]
    )


# --------------------------------------------------------------------------- summary

SUMMARY_SYSTEM = """You maintain a concise running "story so far" for a translator of a Chinese web novel. \
It is given to the translator with every chapter to resolve pronouns, relationships and references.

Merge the new chapter into the existing summary and return the full updated summary:
- at most ~450 words, in English, using the names exactly as they appear in the translation
- a "Key characters" list (name — role, gender, current relationships/status) and a short "Recent events" section
- keep the most recent chapters in more detail; compress or drop old, resolved events
- plain text with simple "-" bullets, no commentary"""

SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
    "additionalProperties": False,
}


def build_summary_prompt(old_summary: str, chapter_label: str, translation: str) -> str:
    return "\n\n".join(
        [
            "## Existing summary\n" + (old_summary.strip() or "(empty — this is the first chapter)"),
            f"## New chapter: {chapter_label}\n" + translation.strip(),
        ]
    )


# --------------------------------------------------------------------------- connection test

PING_SYSTEM = "Reply with the requested JSON."
PING_SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
    "additionalProperties": False,
}


# --------------------------------------------------------------------------- explain a word (reading help)

EXPLAIN_SYSTEM = """You help a reader who speaks Mandarin fluently but reads Chinese less well. They are reading a \
Chinese web novel and want to understand one word or phrase in its sentence. Typical requests are literary or formal \
words (书面语), idioms (成语), classical phrasing and genre jargon (cultivation realms, techniques, sect titles).

Answer briefly and concretely:
- `pinyin`: with tone marks, for the word exactly as written
- `meaning`: a short English gloss for THIS context (a few words)
- `explanation_zh`: a plain spoken-Mandarin paraphrase (大白话), one or two short sentences, as you would explain it \
to a native speaker who doesn't recognise the written form
- `explanation_en`: one to three sentences on what it means here, any nuance or connotation, and how the genre uses \
it. If it is a 成语, give the literal meaning and the source story in one sentence.
- `breakdown`: what each character (or meaningful part) contributes, e.g. "筑 build + 基 foundation". Empty for a \
single character.
- `examples`: up to two short, natural example sentences using it (zh with en), different from the given sentence. \
Empty if it is a name unique to the story.
- `is_story_term`: true if it is a name or term specific to this novel rather than general Chinese."""

EXPLAIN_SCHEMA = {
    "type": "object",
    "properties": {
        "pinyin": {"type": "string"},
        "meaning": {"type": "string"},
        "explanation_zh": {"type": "string"},
        "explanation_en": {"type": "string"},
        "breakdown": {"type": "string"},
        "examples": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"zh": {"type": "string"}, "en": {"type": "string"}},
                "required": ["zh", "en"],
                "additionalProperties": False,
            },
        },
        "is_story_term": {"type": "boolean"},
    },
    "required": ["pinyin", "meaning", "explanation_zh", "explanation_en", "breakdown", "examples", "is_story_term"],
    "additionalProperties": False,
}


def build_explain_prompt(word: str, sentence: str, novel: str = "", dictionary_hint: str = "", glossary_hint: str = "") -> str:
    parts = [f"## Word\n{word}", f"## Sentence\n{sentence.strip() or '(no context)'}"]
    if novel:
        parts.append(f"## Novel\n{novel}")
    if glossary_hint:
        parts.append(f"## The translator's glossary entry\n{glossary_hint}")
    if dictionary_hint:
        parts.append(f"## Dictionary (CC-CEDICT) says\n{dictionary_hint}")
    return "\n\n".join(parts)
