"""Frame A / Frame B text editors: term highlighting, tooltips, paragraph linking, context menus."""

from __future__ import annotations

import re

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QColor, QFont, QSyntaxHighlighter, QTextCharFormat, QTextCursor, QTextFormat
from PySide6.QtWidgets import QMenu, QPlainTextEdit, QTextEdit, QToolTip

from ..glossary import Glossary
from ..models import GlossaryEntry, Warning
from ..services import UNTRANSLATED_MARK


def _fmt(underline: QColor | None = None, background: QColor | None = None, color: QColor | None = None,
         style=QTextCharFormat.SingleUnderline) -> QTextCharFormat:
    f = QTextCharFormat()
    if underline is not None:
        f.setUnderlineStyle(style)
        f.setUnderlineColor(underline)
    if background is not None:
        f.setBackground(background)
    if color is not None:
        f.setForeground(color)
    return f


PARAGRAPH_SEPARATOR = chr(0x2029)  # what QTextCursor.selectedText() puts between paragraphs

CONFIRMED = _fmt(underline=QColor("#2f80ed"))
SUGGESTED = _fmt(underline=QColor("#2f80ed"), style=QTextCharFormat.DotLine)
WARNED = _fmt(background=QColor(255, 165, 0, 90))
MISSING = _fmt(background=QColor(235, 87, 87, 70))
UNTRANSLATED = _fmt(background=QColor(235, 87, 87, 110))


class TermHighlighter(QSyntaxHighlighter):
    """mode='zh' highlights Chinese glossary/warning terms; mode='en' highlights English renderings."""

    def __init__(self, document, mode: str):
        super().__init__(document)
        self.mode = mode
        self.glossary = Glossary()
        self.warn_rx: re.Pattern | None = None
        self.missing_rx: re.Pattern | None = None
        self.en_rx: re.Pattern | None = None
        self.en_map: dict[str, GlossaryEntry] = {}

    @staticmethod
    def _alt(terms, flags=0, words=False) -> re.Pattern | None:
        terms = sorted({t for t in terms if t and t.strip()}, key=len, reverse=True)
        if not terms:
            return None
        parts = []
        for t in terms:
            p = re.escape(t)
            if words:
                p = (r"\b" if t[0].isalnum() else "") + p + (r"\b" if t[-1].isalnum() else "")
            parts.append(p)
        return re.compile("|".join(parts), flags)

    def set_data(self, glossary: Glossary, warnings: list[Warning]) -> None:
        self.glossary = glossary
        uncertain = [w for w in warnings if w.kind == "uncertain"]
        missing = [w for w in warnings if w.kind == "missing"]
        if self.mode == "zh":
            self.warn_rx = self._alt([w.zh for w in uncertain])
            self.missing_rx = self._alt([w.zh for w in missing])
        else:
            self.warn_rx = self._alt([w.suggested_en for w in uncertain], re.IGNORECASE, words=True)
            self.missing_rx = None
            self.en_map = {e.en.lower(): e for e in glossary.entries if e.en}
            self.en_rx = self._alt([e.en for e in glossary.entries], re.IGNORECASE, words=True)
        self.rehighlight()

    def highlightBlock(self, text: str) -> None:  # noqa: N802
        if not text:
            return
        if self.mode == "zh":
            for s, e, entry in self.glossary.matches(text):
                self.setFormat(s, e - s, CONFIRMED if entry.confirmed else SUGGESTED)
        else:
            if self.en_rx:
                for m in self.en_rx.finditer(text):
                    entry = self.en_map.get(m.group(0).lower())
                    self.setFormat(m.start(), m.end() - m.start(), CONFIRMED if entry and entry.confirmed else SUGGESTED)
            if text.startswith(UNTRANSLATED_MARK):
                self.setFormat(0, len(text), UNTRANSLATED)
        if self.missing_rx:
            for m in self.missing_rx.finditer(text):
                self._merge(m.start(), m.end(), MISSING)
        if self.warn_rx:
            for m in self.warn_rx.finditer(text):
                self._merge(m.start(), m.end(), WARNED)

    def _merge(self, start: int, end: int, extra: QTextCharFormat) -> None:
        for i in range(start, end):
            f = QTextCharFormat(self.format(i))
            f.merge(extra)
            self.setFormat(i, 1, f)


class TextFrame(QPlainTextEdit):
    paragraphActivated = Signal(object)  # paragraph index or None
    addTermRequested = Signal(str, str)  # selected text, side ("zh" | "en")
    retranslateRequested = Signal(int)  # paragraph index
    findInGlossaryRequested = Signal(str)
    readAloudRequested = Signal()
    stopReadingRequested = Signal()
    explainRequested = Signal()  # explain the selection, or the word at the text cursor
    starRequested = Signal()

    def __init__(self, side: str, placeholder: str):
        super().__init__()
        self.side = side
        self.setPlaceholderText(placeholder)
        self.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        self.highlighter = TermHighlighter(self.document(), side)
        self._linked_paragraph: int | None = None
        self._suppress = False
        self._speech_range: tuple[int, int] | None = None  # (doc position, length) of the word being read aloud
        self.is_speaking = lambda: False  # set by the main window
        # Pop-up dictionary hooks (Frame A only), set by the main window:
        #   hover_lookup(block, text, pos, glossary_match) -> (tooltip_html, start, end) in block coordinates, or None
        #   word_at(text, pos) -> the dictionary word at pos, for menu labels
        self.hover_lookup = None
        self.word_at = None
        self._lookup_range: tuple[int, int] | None = None  # (doc position, length) of the looked-up word
        if side == "zh":
            self.setMouseTracking(True)
        self.cursorPositionChanged.connect(self._on_cursor)

    # ----- helpers ----------------------------------------------------------
    def set_font_size(self, size: int) -> None:
        f = QFont(self.font())
        f.setPointSize(size)
        self.setFont(f)

    def set_text_undoable(self, text: str) -> None:
        """Replace all text while keeping Ctrl+Z history."""
        cur = QTextCursor(self.document())
        cur.beginEditBlock()
        cur.select(QTextCursor.Document)
        cur.insertText(text)
        cur.endEditBlock()

    def load_text(self, text: str) -> None:
        """Replace all text and reset undo history (used when switching chapters)."""
        self._suppress = True
        self.setPlainText(text)
        self._suppress = False

    def paragraph_of_block(self, block) -> int | None:
        if not block.isValid() or not block.text().strip():
            return None
        n, b = 0, self.document().firstBlock()
        while b.isValid() and b != block:
            if b.text().strip():
                n += 1
            b = b.next()
        return n

    def block_of_paragraph(self, index: int):
        n, b = -1, self.document().firstBlock()
        while b.isValid():
            if b.text().strip():
                n += 1
                if n == index:
                    return b
            b = b.next()
        return None

    def current_paragraph(self) -> int | None:
        return self.paragraph_of_block(self.textCursor().block())

    def text_to_read(self) -> tuple[int, str]:
        """The selection, or the paragraph under the cursor if nothing is selected, with its document position."""
        cur = self.textCursor()
        if not cur.hasSelection():
            block = cur.block()
            return block.position(), block.text()
        start = cur.selectionStart()
        # selectedText() uses U+2029 between paragraphs; keep lengths equal so word offsets map back to the document
        return start, cur.selectedText().replace(PARAGRAPH_SEPARATOR, "\n")
    def set_speech_range(self, start: int | None, length: int = 0) -> None:
        self._speech_range = None if start is None else (start, length)
        if start is not None:  # scroll the spoken word into view without moving the user's cursor/selection
            cur = QTextCursor(self.document())
            cur.setPosition(min(start, self.document().characterCount() - 1))
            if not self.viewport().rect().contains(self.cursorRect(cur)):
                self._suppress = True
                saved = self.textCursor()
                self.setTextCursor(cur)
                self.centerCursor()
                self.setTextCursor(saved)
                self._suppress = False
        self._update_selections()

    # ----- paragraph linking -----------------------------------------------
    def _on_cursor(self) -> None:
        self._update_selections()
        if not self._suppress:
            self.paragraphActivated.emit(self.current_paragraph())

    def show_linked_paragraph(self, index: int | None, move: bool = True) -> None:
        self._linked_paragraph = index
        if index is not None and move:
            block = self.block_of_paragraph(index)
            if block is not None:
                self._suppress = True
                cur = QTextCursor(block)
                self.setTextCursor(cur)
                self.centerCursor()
                self._suppress = False
        self._update_selections()

    def _update_selections(self) -> None:
        sels = []
        idx = self._linked_paragraph
        if idx is None and self.hasFocus():
            idx = self.current_paragraph()
        if idx is not None:
            block = self.block_of_paragraph(idx)
            if block is not None:
                sel = QTextEdit.ExtraSelection()
                sel.format.setBackground(QColor(47, 128, 237, 40))
                sel.format.setProperty(QTextFormat.FullWidthSelection, True)
                sel.cursor = QTextCursor(block)
                sels.append(sel)
        if self._lookup_range is not None:
            start, length = self._lookup_range
            sel = QTextEdit.ExtraSelection()
            sel.format.setBackground(QColor(242, 201, 76, 110))
            cur = QTextCursor(self.document())
            end = self.document().characterCount() - 1
            cur.setPosition(min(start, end))
            cur.setPosition(min(start + length, end), QTextCursor.KeepAnchor)
            sel.cursor = cur
            sels.append(sel)
        if self._speech_range is not None:
            start, length = self._speech_range
            sel = QTextEdit.ExtraSelection()
            sel.format.setBackground(QColor(39, 174, 96, 110))
            cur = QTextCursor(self.document())
            end = self.document().characterCount() - 1
            cur.setPosition(min(start, end))
            cur.setPosition(min(start + length, end), QTextCursor.KeepAnchor)
            sel.cursor = cur
            sels.append(sel)
        self.setExtraSelections(sels)

    def focusInEvent(self, e):  # noqa: N802
        self._linked_paragraph = None
        super().focusInEvent(e)
        self._update_selections()

    # ----- tooltips & pop-up dictionary -----------------------------------
    def char_at(self, point) -> tuple[object, int] | None:
        """(block, index in block) of the character under `point`, or None if the point is not over text."""
        cur = self.cursorForPosition(point)
        block = cur.block()
        pos = cur.positionInBlock()
        rect = self.cursorRect(cur)
        if point.x() < rect.x() and pos > 0:  # cursorForPosition picks the nearest gap; we want the glyph under the mouse
            pos -= 1
        if not (0 <= pos < len(block.text())):
            return None
        c = QTextCursor(block)
        c.setPosition(block.position() + pos)
        left = self.cursorRect(c)
        c.setPosition(block.position() + pos + 1)
        right = self.cursorRect(c)
        if right.y() == left.y() and not (left.x() - 1 <= point.x() <= right.x() + 1):
            return None  # past the end of a line
        if not (left.top() - 2 <= point.y() <= left.bottom() + 2):
            return None
        return block, pos

    def set_lookup_range(self, start: int | None, length: int = 0) -> None:
        new = None if start is None else (start, length)
        if new != self._lookup_range:
            self._lookup_range = new
            self._update_selections()

    def event(self, e):
        if e.type() == QEvent.ToolTip:
            hit = self.char_at(e.pos())
            if hit is None:
                QToolTip.hideText()
                self.set_lookup_range(None)
                return True
            block, pos = hit
            text = block.text()
            gmatch = self._glossary_match_at(text, pos)
            html = None
            if self.side == "zh" and self.hover_lookup is not None:
                found = self.hover_lookup(block, text, pos, gmatch)
                if found:
                    html, start, end = found
                    self.set_lookup_range(block.position() + start, end - start)
            if html is None and gmatch:
                html = self._glossary_html(gmatch[2])
            if html:
                QToolTip.showText(e.globalPos(), html, self)
            else:
                QToolTip.hideText()
                self.set_lookup_range(None)
            return True
        if e.type() == QEvent.Leave:
            self.set_lookup_range(None)
        return super().event(e)

    def mouseMoveEvent(self, e):  # noqa: N802
        super().mouseMoveEvent(e)
        if self._lookup_range is not None:
            hit = self.char_at(e.position().toPoint())
            start, length = self._lookup_range
            if hit is None or not (start <= hit[0].position() + hit[1] < start + length):
                self.set_lookup_range(None)

    @staticmethod
    def _glossary_html(entry: GlossaryEntry) -> str:
        lines = [f"<b>{entry.zh}</b> → <b>{entry.en}</b>", f"{entry.category}"
                 + (f", {entry.gender}" if entry.gender else "")
                 + ("" if entry.confirmed else " (unconfirmed)")]
        if entry.aliases:
            lines.append("Aliases: " + ", ".join(entry.aliases))
        if entry.notes:
            lines.append(entry.notes)
        return "<br>".join(lines)

    def _glossary_match_at(self, text: str, pos: int) -> tuple[int, int, GlossaryEntry] | None:
        h = self.highlighter
        if self.side == "zh":
            for s, end, entry in h.glossary.matches(text):
                if s <= pos < end:
                    return s, end, entry
        elif h.en_rx:
            for m in h.en_rx.finditer(text):
                if m.start() <= pos < m.end():
                    entry = h.en_map.get(m.group(0).lower())
                    return (m.start(), m.end(), entry) if entry else None
        return None

    def selected_or_cursor(self) -> tuple[str, object, int]:
        """(selected text or '', block, index in block) — the selection start, or the text cursor position."""
        cur = self.textCursor()
        sel = cur.selectedText().replace(PARAGRAPH_SEPARATOR, "\n").strip() if cur.hasSelection() else ""
        c = QTextCursor(self.document())
        c.setPosition(cur.selectionStart())
        return sel, c.block(), c.positionInBlock()

    # ----- context menu -----------------------------------------------------
    def contextMenuEvent(self, e):  # noqa: N802
        if not self.textCursor().hasSelection():  # right-click places the cursor, so "read paragraph" uses the clicked one
            hit = self.char_at(e.pos())
            if hit is not None:  # exactly before the clicked character, so "Explain" uses the word under the mouse
                cur = QTextCursor(self.document())
                cur.setPosition(hit[0].position() + hit[1])
                self.setTextCursor(cur)
            else:
                self.setTextCursor(self.cursorForPosition(e.pos()))
        menu: QMenu = self.createStandardContextMenu()
        selected = self.textCursor().selectedText().strip()
        menu.addSeparator()
        if self.side == "zh" and self.word_at is not None:
            target = selected if selected and len(selected) <= 20 else None
            if target is None and not selected:
                _sel, block, pos = self.selected_or_cursor()
                target = self.word_at(block.text(), pos)
            if target:
                menu.addAction(f"Explain “{target}”\tCtrl+E", self.explainRequested.emit)
                menu.addAction(f"Star “{target}” in word list", self.starRequested.emit)
                menu.addSeparator()
        if selected and len(selected) <= 60:
            menu.addAction(f"Add “{selected}” to glossary…", lambda: self.addTermRequested.emit(selected, self.side))
            menu.addAction(f"Find “{selected}” in glossary", lambda: self.findInGlossaryRequested.emit(selected))
        if self.is_speaking():
            menu.addAction("Stop reading aloud", self.stopReadingRequested.emit)
        else:
            what = "selection" if selected else "paragraph"
            menu.addAction(f"Read {what} aloud", self.readAloudRequested.emit)
        para = self.paragraph_of_block(self.cursorForPosition(e.pos()).block())
        if para is not None:
            menu.addAction(f"Retranslate paragraph {para + 1}…", lambda: self.retranslateRequested.emit(para))
        menu.exec(e.globalPos())
