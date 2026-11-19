"""Application-wide settings (not per novel), stored in the user's config folder."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .text_utils import DEFAULT_CLEANUP_PATTERNS


def config_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / ".config")
    return Path(base) / "LNTranslator"


@dataclass
class Settings:
    claude_path: str = ""  # empty = auto-detect
    model: str = ""  # empty = Claude Code default; e.g. "opus", "sonnet", "haiku" or a full model id
    effort: str = ""  # empty = default; low|medium|high|xhigh|max
    summary_effort: str = "low"
    chunk_chars: int = 3000
    timeout_seconds: int = 900
    update_summary: bool = True
    context_paragraphs: int = 3
    cleanup_patterns: list[str] = field(default_factory=lambda: list(DEFAULT_CLEANUP_PATTERNS))
    recent_projects: list[str] = field(default_factory=list)
    font_size: int = 11
    tts_voice_zh: str = ""  # empty = first Chinese voice
    tts_voice_en: str = ""  # empty = first English voice
    tts_rate: int = 0  # -10 (slow) .. 10 (fast)

    @classmethod
    def load(cls) -> "Settings":
        path = config_dir() / "settings.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return cls()
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    def save(self) -> None:
        d = config_dir()
        d.mkdir(parents=True, exist_ok=True)
        (d / "settings.json").write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")

    def add_recent(self, path: str) -> None:
        path = str(Path(path))
        self.recent_projects = [path] + [p for p in self.recent_projects if p != path]
        self.recent_projects = self.recent_projects[:10]
