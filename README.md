# LN Translator

A desktop app (Python + PySide6) for translating Chinese web novels into English, using **Claude Code** as the
translation AI and a per-novel terminology glossary for consistency.

## Run

```
run.bat                                  # or:
.venv\Scripts\python -m ln_translator
```

Requirements: Python 3.11+, `pip install -r requirements.txt`, and Claude Code installed and logged in
(run `claude` once in a terminal). The app uses your Claude Code login, so there's no API key. Usage counts
against your Claude plan, and the status bar shows your 5-hour and 7-day plan usage after each call.

## Workflow

1. **File → New project**: one folder per novel.
2. **Sync (F5)**: paste a Chinese chapter into **Frame A** and an existing English translation into **Frame B**.
   Claude extracts the terminology. You review additions, updates and conflicts before anything changes, and you can
   add the translator's style notes to the style guide.
3. **Translate (F6)**: paste a new chapter into Frame A. Only the glossary terms that occur in the chapter are sent,
   together with the style guide and the rolling story summary. Long chapters are split into chunks, and each chunk
   gets the previous translated paragraphs as context.
4. **Warnings tab**: lists terms Claude was unsure about (orange) and glossary terms missing from the output (red).
   **Accept suggestion** or **Add to glossary…** adds them in one click.
5. **Direct vs. indirect names**: in the **Style & naming** tab, choose for each category whether new unique
   names are translated by **meaning** (蓝月 → Blue Moon), in **pinyin** (南岳 → Nan Yue), as **pinyin + translated
   suffix** (南岳山 → Nanyue Mountain), or **Claude decides**. Glossary entries always win. Every uncertain-term warning
   also lists the other rendering. **Use alternative** adds that rendering to the glossary and swaps it into Frame B
   (Ctrl+Z undoes it).
6. **Refine**:
   - **Fix flagged (F8)** retranslates the paragraphs with missing terms.
   - **Retranslate selection (F7)** retranslates the selected paragraphs and takes an optional instruction
     ("more formal", "she is speaking").
   - You can edit Frame B by hand and then Sync again, so the glossary learns from your edits.
   - Every replaced version is kept: **Chapter → Translation history**, **Glossary → Glossary history**.

### Other features

- **Glossary:** hover a term to see its glossary entry. Right-click selected text to add it to the glossary.
  Search and filter the glossary (category, unconfirmed, "in this chapter"). Import and export as CSV or JSON.
- **Confirmed terms:** a ticked ✓ term is never overwritten by Sync. Editing a term confirms it.
- **Paragraph linking:** when both frames have the same number of paragraphs, clicking a paragraph highlights its
  partner in the other frame.
- **Raw text:** **Clean raw** removes site watermarks and ads (the patterns are editable in Settings).
  **Traditional → Simplified** converts the Chinese text.
- **Files:** load chapter text from `.txt` files (UTF-8, GB18030 and Big5 are detected), or export all translations
  to a single `.txt`.
- **Settings:** model, effort, chunk size, context paragraphs, automatic summary updates, timeout and editor font.

## Project folder

```
MyNovel/
  project.json  glossary.json  style.md  summary.md
  chapters/0001/raw.txt  translation.txt  meta.json (title, warnings)
  history/chapters/…  history/glossary/…  history/deleted/…
```

These are plain files, so you can back them up or open the folder in Claude Code and ask questions about the whole novel.

## How Claude Code is called

`ln_translator/claude_bridge.py` runs a separate one-off session for every operation:

```
claude.exe -p --output-format stream-json --verbose --include-partial-messages
           --tools "" --strict-mcp-config --disable-slash-commands --no-session-persistence
           --system-prompt <translator prompt> --json-schema <schema> [--model …] [--effort …]
```

- The prompt goes through stdin, which avoids the Windows command-line length limit.
- The result is read from `structured_output`.
- The npm `claude.cmd` shim is bypassed in favour of the real `claude.exe`, because cmd.exe would mangle the
  arguments.
- `--strict-mcp-config` keeps your MCP servers out of these calls, which saves about 10K tokens per call.

## Code layout

| Module | Purpose |
|---|---|
| `models.py` | Glossary entries, warnings, Sync proposals |
| `text_utils.py` | Paragraphs, alignment, chunking, raw cleanup |
| `glossary.py` | Longest-match term search, consistency check, Sync diff/merge, CSV/JSON |
| `project.py` | Project folder storage and version history |
| `prompts.py` | System prompts, prompt builders, JSON schemas |
| `claude_bridge.py` | Headless Claude Code runner |
| `services.py` | Translate / Retranslate / Sync / Summary operations |
| `ui/` | PySide6 window, editors, glossary table, dialogs, background workers |

Tests: `.venv\Scripts\python -m pytest tests`
