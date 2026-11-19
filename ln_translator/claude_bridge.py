"""Runs Claude Code in headless mode (`claude -p`) and returns structured JSON.

Each call is an isolated, tool-less, non-persisted session:
  * the prompt is written to stdin (Windows command lines are limited to ~32K chars)
  * --json-schema forces a structured result, read from the final `result` event
  * --strict-mcp-config skips the user's MCP servers (saves ~10K+ tokens per call)
  * --tools "" means Claude can only read the prompt and answer
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

ProgressFn = Callable[[str], None]


class ClaudeError(RuntimeError):
    pass


class Cancelled(Exception):
    pass


@dataclass
class CallStats:
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    duration_s: float = 0.0
    five_hour_utilization: float | None = None
    seven_day_utilization: float | None = None
    rate_limit_status: str = ""


@dataclass
class ClaudeResult:
    data: dict
    stats: CallStats = field(default_factory=CallStats)


def resolve_claude_executable(configured: str = "") -> str:
    """Find a directly executable Claude Code binary.

    npm installs a `claude.cmd` shim; running it goes through cmd.exe, which mangles
    multi-line/quoted arguments, so prefer the real claude.exe behind it.
    """
    candidates = []
    if configured:
        candidates.append(configured)
    found = shutil.which("claude")
    if found:
        candidates.append(found)
    home = Path.home()
    candidates += [
        str(home / ".local" / "bin" / "claude.exe"),
        str(home / ".local" / "bin" / "claude"),
        str(home / ".claude" / "local" / "claude"),
    ]
    for c in candidates:
        p = Path(c)
        if not p.exists():
            continue
        if p.suffix.lower() in {".cmd", ".bat", ".ps1"} or (os.name == "nt" and p.suffix == ""):
            real = p.parent / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
            if real.exists():
                return str(real)
            if p.suffix.lower() in {".cmd", ".bat", ".ps1"} or not p.with_suffix(".exe").exists():
                continue
            return str(p.with_suffix(".exe"))
        return str(p)
    raise ClaudeError(
        "Claude Code was not found. Install it (https://claude.com/claude-code), run `claude` once to log in, "
        "or set its path in Settings."
    )


class ClaudeBridge:
    def __init__(
        self,
        claude_path: str = "",
        model: str = "",
        effort: str = "",
        timeout_seconds: int = 900,
    ):
        self.claude_path = claude_path
        self.model = model
        self.effort = effort
        self.timeout_seconds = timeout_seconds
        self._workdir = Path(tempfile.gettempdir()) / "ln_translator_claude"
        self._workdir.mkdir(exist_ok=True)

    def build_command(self, system_prompt: str, schema: dict, effort: str | None = None) -> list[str]:
        cmd = [
            resolve_claude_executable(self.claude_path),
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--tools",
            "",
            "--strict-mcp-config",
            "--disable-slash-commands",
            "--no-session-persistence",
            "--system-prompt",
            system_prompt,
            "--json-schema",
            json.dumps(schema, ensure_ascii=False),
        ]
        if self.model:
            cmd += ["--model", self.model]
        effort = self.effort if effort is None else effort
        if effort:
            cmd += ["--effort", effort]
        return cmd

    def run(
        self,
        prompt: str,
        system_prompt: str,
        schema: dict,
        progress: ProgressFn | None = None,
        cancel: threading.Event | None = None,
        effort: str | None = None,
        label: str = "Claude",
    ) -> ClaudeResult:
        progress = progress or (lambda _m: None)
        cancel = cancel or threading.Event()
        cmd = self.build_command(system_prompt, schema, effort)
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(self._workdir),
                encoding="utf-8",
                errors="replace",
                creationflags=creationflags,
            )
        except FileNotFoundError as e:
            raise ClaudeError(f"Could not start Claude Code: {e}") from e

        stderr_lines: list[str] = []
        threading.Thread(target=lambda: stderr_lines.extend(proc.stderr), daemon=True).start()

        def feed():
            try:
                proc.stdin.write(prompt)
                proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass

        threading.Thread(target=feed, daemon=True).start()

        # Watchdog: kill on cancel or timeout.
        start = time.monotonic()
        done = threading.Event()

        def watchdog():
            while not done.wait(0.25):
                if cancel.is_set() or time.monotonic() - start > self.timeout_seconds:
                    proc.kill()
                    return

        threading.Thread(target=watchdog, daemon=True).start()

        stats = CallStats()
        final: dict | None = None
        received_chars = 0
        last_report = 0.0
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                etype = ev.get("type")
                if etype == "stream_event":
                    delta = (ev.get("event") or {}).get("delta") or {}
                    chunk = delta.get("partial_json") or delta.get("text") or ""
                    received_chars += len(chunk)
                    now = time.monotonic()
                    if chunk and now - last_report > 0.5:
                        last_report = now
                        progress(f"{label}: writing… {received_chars:,} chars received")
                elif etype == "system" and ev.get("subtype") == "thinking_tokens":
                    progress(f"{label}: thinking… (~{ev.get('estimated_tokens', 0):,} tokens)")
                elif etype == "system" and ev.get("subtype") == "init":
                    progress(f"{label}: started ({ev.get('model', '')})")
                elif etype == "rate_limit_event":
                    info = ev.get("rate_limit_info") or {}
                    stats.rate_limit_status = info.get("status", "")
                    windows = info.get("unifiedWindows") or {}
                    if "five_hour" in windows:
                        stats.five_hour_utilization = windows["five_hour"].get("utilization")
                    if "seven_day" in windows:
                        stats.seven_day_utilization = windows["seven_day"].get("utilization")
                elif etype == "result":
                    final = ev
        finally:
            proc.wait()
            done.set()

        stats.duration_s = time.monotonic() - start
        if cancel.is_set():
            raise Cancelled()
        if final is None:
            if stats.duration_s > self.timeout_seconds:
                raise ClaudeError(f"Claude Code timed out after {self.timeout_seconds}s.")
            err = "".join(stderr_lines).strip()
            raise ClaudeError(f"Claude Code exited without a result (code {proc.returncode}). {err[-2000:]}")

        usage = final.get("usage") or {}
        stats.cost_usd = float(final.get("total_cost_usd") or 0)
        stats.input_tokens = int(usage.get("input_tokens") or 0) + int(usage.get("cache_read_input_tokens") or 0) + int(
            usage.get("cache_creation_input_tokens") or 0
        )
        stats.output_tokens = int(usage.get("output_tokens") or 0)

        if final.get("is_error") or final.get("subtype") not in (None, "success"):
            msg = final.get("result") or final.get("subtype") or "unknown error"
            if final.get("api_error_status"):
                msg = f"{msg} (API status {final['api_error_status']})"
            if stats.rate_limit_status and stats.rate_limit_status != "allowed":
                msg += " — you may have reached your Claude usage limit; try again later."
            raise ClaudeError(f"Claude Code reported an error: {msg}")

        data = final.get("structured_output")
        if data is None:
            try:
                data = json.loads(final.get("result") or "")
            except (json.JSONDecodeError, TypeError):
                raise ClaudeError(
                    "Claude did not return structured output. "
                    f"stop_reason={final.get('stop_reason')}; result={str(final.get('result'))[:500]}"
                )
        if not isinstance(data, dict):
            raise ClaudeError("Claude returned an unexpected result shape.")
        return ClaudeResult(data=data, stats=stats)
