"""Pop-up dictionary rendering (tooltip + side panel) and the word-list panel."""

from __future__ import annotations

from html import escape

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ..dictionary import DictEntry, Dictionary, LookupResult, SavedWord, WordList, WordMatch
from ..models import GlossaryEntry

MUTED = "color:#888"
ACCENT = "color:#2f80ed"


def _hl_sentence(sentence: str, word: str) -> str:
    s = escape(sentence)
    w = escape(word)
    return s.replace(w, f"<b style='{ACCENT}'>{w}</b>", 1) if w else s


def _pinyins(entries: list[DictEntry]) -> str:
    seen: list[str] = []
    for e in entries:
        if e.pinyin.lower() not in (x.lower() for x in seen):
            seen.append(e.pinyin)
    return " / ".join(seen[:3])


def _glossary_line(g: GlossaryEntry) -> str:
    extra = ", ".join(x for x in (g.category, g.gender) if x)
    return f"📘 <b>{escape(g.en)}</b>" + (f" <span style='{MUTED}'>({escape(extra)})</span>" if extra else "")


# ------------------------------------------------------------------ tooltip (compact)
def tooltip_html(word: str, result: LookupResult | None, glossary: GlossaryEntry | None, saved: SavedWord | None,
                 entries: list[DictEntry], pinyin_hint: str = "") -> str:
    rows = [f"<span style='font-size:16pt'>{escape(word)}</span>&nbsp; <b>{escape(_pinyins(entries) or pinyin_hint)}</b>"]
    if glossary:
        rows.append(_glossary_line(glossary))
    if saved and saved.explanation:
        rows.append(f"💡 {escape(saved.explanation.get('meaning', ''))}")
    for e in entries[:3]:
        py = f"<span style='{MUTED}'>{escape(e.pinyin)}</span> " if len({x.pinyin for x in entries}) > 1 else ""
        rows.append(py + escape(e.short_gloss(90)))
    if not entries and not glossary:
        rows.append(f"<span style='{MUTED}'>Not in the dictionary. Ctrl+E asks Claude.</span>")
    if result:
        if result.chars and len(result.chars) <= 4:
            parts = [f"{escape(c.word)} {escape(c.entries[0].pinyin)} <span style='{MUTED}'>{escape(c.entries[0].short_gloss(22))}</span>"
                     for c in result.chars if c.entries]
            if parts:
                rows.append("<hr>" + " · ".join(parts))
        in_breakdown = {c.word for c in result.chars} if len(result.chars) <= 4 else set()
        others = [m for m in result.shorter if m.entries and m.word not in in_breakdown]
        if result.container and result.container.word != word:
            others.insert(0, result.container)
        if result.primary.word != word and result.primary.entries:  # glossary term won; still show the dictionary word
            others.insert(0, result.primary)
        for m in others[:3]:
            rows.append(f"<span style='{MUTED}'>{escape(m.word)} {escape(m.entries[0].pinyin)}: {escape(m.entries[0].short_gloss(50))}</span>")
    if saved and saved.saved:
        rows.append(f"<span style='{MUTED}'>★ in your word list</span>")
    return "<div style='max-width:420px'>" + "<br>".join(rows) + "</div>"


# ------------------------------------------------------------------ side panel (full)
def panel_html(word: str, dictionary: Dictionary | None, result: LookupResult | None, glossary: GlossaryEntry | None,
               saved: SavedWord | None, sentence: str, status: str = "") -> str:
    entries = dictionary.lookup(word) if dictionary else []
    pinyin = _pinyins(entries) or (saved.pinyin if saved else "") or (dictionary.guess_pinyin(word) if dictionary else "")
    h = [f"<p><span style='font-size:24pt'>{escape(word)}</span>&nbsp;&nbsp;"
         f"<span style='font-size:13pt'><b>{escape(pinyin)}</b></span>"
         + ("&nbsp; ★" if saved and saved.saved else "") + "</p>"]
    if status:
        h.append(f"<p style='{MUTED}'><i>{escape(status)}</i></p>")
    if glossary:
        h.append("<p>" + _glossary_line(glossary) + (f"<br><span style='{MUTED}'>{escape(glossary.notes)}</span>" if glossary.notes else "") + "</p>")

    ex = saved.explanation if saved else None
    if ex:
        h.append(f"<h4>💡 {escape(ex.get('meaning', ''))}</h4>")
        if ex.get("explanation_zh"):
            h.append(f"<p style='font-size:12pt'>{escape(ex['explanation_zh'])}</p>")
        if ex.get("explanation_en"):
            h.append(f"<p>{escape(ex['explanation_en'])}</p>")
        if ex.get("breakdown"):
            h.append(f"<p style='{MUTED}'>{escape(ex['breakdown'])}</p>")
        for e in ex.get("examples") or []:
            h.append(f"<p>• {escape(e.get('zh', ''))}<br><span style='{MUTED}'>&nbsp;&nbsp;{escape(e.get('en', ''))}</span></p>")
        if ex.get("context") and ex.get("context") != sentence:
            h.append(f"<p style='{MUTED}'>Explained for: {_hl_sentence(ex['context'], word)}</p>")

    if entries:
        h.append("<h4>Dictionary</h4><ol>")
        for e in entries:
            h.append(f"<li><b>{escape(e.pinyin)}</b>"
                     + (f" <span style='{MUTED}'>[{escape(e.traditional)}]</span>" if e.traditional != word else "")
                     + "<br>" + escape("; ".join(e.definitions)) + "</li>")
        h.append("</ol>")
    elif not glossary and not ex:
        h.append(f"<p style='{MUTED}'>Not in CC-CEDICT — often a literary phrase or a term coined by the novel. "
                 "Press <b>Explain</b> (Ctrl+E) to ask Claude.</p>")

    if result and result.word == word:
        chars = result.chars
    elif dictionary and len(word) > 1:
        chars = [WordMatch(c, i, i + 1, dictionary.lookup(c)) for i, c in enumerate(word)]
    else:
        chars = []
    chars = [c for c in chars if c.entries]
    if chars:
        h.append("<h4>Characters</h4><p>" + "<br>".join(
            f"<span style='font-size:13pt'>{escape(c.word)}</span> <b>{escape(c.entries[0].pinyin)}</b> {escape(c.entries[0].short_gloss(60))}"
            for c in chars) + "</p>")

    if result:
        others = []
        if result.primary.word != word and result.primary.entries:
            others.append(result.primary)
        if result.container and result.container.word != word:
            others.append(result.container)
        others += [m for m in result.shorter if m.entries and m.word != word]
        if others:
            h.append("<h4>Related</h4><p>" + "<br>".join(
                f"{escape(m.word)} <b>{escape(m.entries[0].pinyin)}</b> {escape(m.entries[0].short_gloss(60))}" for m in others) + "</p>")

    if sentence:
        h.append(f"<h4>Sentence</h4><p style='font-size:12pt'>{_hl_sentence(sentence, word)}</p>")
    if saved and saved.lookups:
        h.append(f"<p style='{MUTED}'>Looked up {saved.lookups}× · last {escape(saved.last_seen)}"
                 + (f" · {escape(saved.source)}" if saved.source else "") + "</p>")
    return "".join(h)


EMPTY_HTML = ("<p style='color:#888'>Hover a Chinese word in Frame A to look it up.<br><br>"
              "<b>Ctrl+E</b> asks Claude to explain the word (or your selection) in its sentence.<br>"
              "<b>★ Star</b> keeps a word in your list. Words you look up more than once show up there too.</p>")

MISSING_HTML = ("<p><b>The dictionary isn't installed yet.</b></p><p>LN Translator uses CC-CEDICT, a free Chinese–English "
                "dictionary (about 120,000 entries, ~10 MB download, licensed CC BY-SA 4.0). Press "
                "<b>Download dictionary</b> below; it's stored in your settings folder and works offline afterwards.</p>")


class DictionaryPanel(QWidget):
    speakRequested = Signal()
    explainRequested = Signal()
    starRequested = Signal()
    downloadRequested = Signal()
    wordActivated = Signal(str)

    FILTERS = [("Starred, explained or looked up 2×+", "repeat"), ("Starred only", "starred"), ("All lookups", "all")]

    def __init__(self, wordlist: WordList):
        super().__init__()
        self.wordlist = wordlist
        self.view = QTextBrowser()
        self.view.setOpenLinks(False)
        self.view.setHtml(EMPTY_HTML)

        self.b_speak = QPushButton("🔊 Speak")
        self.b_explain = QPushButton("💡 Explain")
        self.b_explain.setToolTip("Ask Claude what this word means in its sentence (Ctrl+E). Answers are saved.")
        self.b_star = QPushButton("☆ Star")
        self.b_download = QPushButton("Download dictionary")
        self.b_download.hide()
        self.b_speak.clicked.connect(self.speakRequested)
        self.b_explain.clicked.connect(self.explainRequested)
        self.b_star.clicked.connect(self.starRequested)
        self.b_download.clicked.connect(self.downloadRequested)
        row = QHBoxLayout()
        for b in (self.b_speak, self.b_explain, self.b_star):
            row.addWidget(b)
        top = QWidget()
        tl = QVBoxLayout(top)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.addWidget(self.view, 1)
        tl.addWidget(self.b_download)
        tl.addLayout(row)

        # --- word list
        self.filter = QComboBox()
        for label, key in self.FILTERS:
            self.filter.addItem(label, key)
        self.filter.currentIndexChanged.connect(self.refresh_list)
        self.count_label = QLabel("")
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Word", "Pinyin", "Meaning", "×"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().hide()
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.Stretch)
        hh.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.table.currentCellChanged.connect(
            lambda r, *_: self.wordActivated.emit(self._row_word(r)) if r >= 0 and self.table.item(r, 0) else None)
        b_remove = QPushButton("Remove")
        b_remove.clicked.connect(self._remove_selected)
        lrow = QHBoxLayout()
        lrow.addWidget(QLabel("<b>Word list</b>"))
        lrow.addWidget(self.filter, 1)
        lrow.addWidget(self.count_label)
        bottom = QWidget()
        bl = QVBoxLayout(bottom)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.addLayout(lrow)
        bl.addWidget(self.table, 1)
        brow = QHBoxLayout()
        brow.addStretch(1)
        brow.addWidget(b_remove)
        bl.addLayout(brow)

        split = QSplitter(Qt.Vertical)
        split.addWidget(top)
        split.addWidget(bottom)
        split.setSizes([520, 300])
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.addWidget(split)
        self.set_word_actions_enabled(False)
        self.refresh_list()

    def set_word_actions_enabled(self, on: bool) -> None:
        for b in (self.b_speak, self.b_explain, self.b_star):
            b.setEnabled(on)

    def set_missing(self, missing: bool) -> None:
        self.b_download.setVisible(missing)
        if missing:
            self.view.setHtml(MISSING_HTML)

    def show_html(self, html: str, starred: bool) -> None:
        self.view.setHtml(html)
        self.b_star.setText("★ Starred" if starred else "☆ Star")
        self.set_word_actions_enabled(True)

    def refresh_list(self) -> None:
        mode = self.filter.currentData()
        words = self.wordlist.sorted(saved_only=mode == "starred")
        if mode == "repeat":
            words = [w for w in words if w.saved or w.explanation or w.lookups >= 2]
        self.table.blockSignals(True)
        self.table.setRowCount(len(words))
        for r, w in enumerate(words):
            vals = [("★ " if w.saved else "") + w.word, w.pinyin, w.gloss, str(w.lookups)]
            for c, v in enumerate(vals):
                item = QTableWidgetItem(v)
                if c == 0:
                    item.setData(Qt.UserRole, w.word)
                    if w.sentence:
                        item.setToolTip(w.sentence)
                self.table.setItem(r, c, item)
        self.table.blockSignals(False)
        self.count_label.setText(f"{len(words)} word{'s' if len(words) != 1 else ''}")

    def _row_word(self, r: int) -> str:
        return self.table.item(r, 0).data(Qt.UserRole)

    def _remove_selected(self) -> None:
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        for r in rows:
            self.wordlist.remove(self._row_word(r))
        if rows:
            self.wordlist.save()
            self.refresh_list()
