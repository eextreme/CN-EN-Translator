"""Dialogs: add/edit term, Sync review, settings, version history."""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..models import CATEGORIES, GENDERS, GlossaryEntry, ProposedChange, split_aliases
from ..project import HistoryItem
from ..settings import Settings


class TermDialog(QDialog):
    def __init__(self, parent: QWidget, entry: GlossaryEntry, title: str = "Glossary term", alternatives: list[str] = ()):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.entry = entry
        form = QFormLayout()
        self.zh = QLineEdit(entry.zh)
        # Editable combo: offers the alternative rendering (meaning vs. pinyin) when there is one.
        self.en = QComboBox()
        self.en.setEditable(True)
        self.en.addItems([x for x in dict.fromkeys([entry.en, *alternatives]) if x])
        self.en.setCurrentText(entry.en)
        if len(alternatives):
            self.en.setToolTip("Pick the meaning-based or pinyin rendering, or type your own")
        self.category = QComboBox()
        self.category.addItems(CATEGORIES)
        self.category.setCurrentText(entry.category if entry.category in CATEGORIES else "other")
        self.aliases = QLineEdit(", ".join(entry.aliases))
        self.aliases.setPlaceholderText("Other Chinese forms, comma separated (e.g. 小动, 林少爷)")
        self.gender = QComboBox()
        self.gender.addItems(GENDERS)
        self.gender.setCurrentText(entry.gender if entry.gender in GENDERS else "")
        self.notes = QLineEdit(entry.notes)
        self.confirmed = QCheckBox("Confirmed (Sync won't overwrite)")
        self.confirmed.setChecked(True)
        form.addRow("Chinese", self.zh)
        form.addRow("English", self.en)
        form.addRow("Category", self.category)
        form.addRow("Aliases", self.aliases)
        form.addRow("Gender", self.gender)
        form.addRow("Notes", self.notes)
        form.addRow("", self.confirmed)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._ok)
        buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(buttons)
        self.resize(460, 0)
        (self.en if entry.zh and not entry.en else self.zh).setFocus()

    def _ok(self) -> None:
        if not self.zh.text().strip() or not self.en.currentText().strip():
            self.zh.setStyleSheet("" if self.zh.text().strip() else "border: 1px solid red")
            self.en.setStyleSheet("" if self.en.currentText().strip() else "border: 1px solid red")
            return
        e = self.entry
        e.zh = self.zh.text().strip()
        e.en = self.en.currentText().strip()
        e.category = self.category.currentText()
        e.aliases = [a for a in split_aliases(self.aliases.text()) if a != e.zh]
        e.gender = self.gender.currentText()
        e.notes = self.notes.text().strip()
        e.confirmed = self.confirmed.isChecked()
        self.accept()


class SyncReviewDialog(QDialog):
    """Lets the user accept/reject/edit each AI-proposed glossary change."""

    HEAD = ["Apply", "Action", "Chinese", "English (editable)", "Current glossary", "Category", "Aliases", "Gender", "Notes"]

    def __init__(self, parent: QWidget, changes: list[ProposedChange], style_observations: list[str]):
        super().__init__(parent)
        self.setWindowTitle("Review Sync results")
        self.changes = changes
        lay = QVBoxLayout(self)
        counts = {a: sum(1 for c in changes if c.action == a) for a in ("add", "update", "conflict")}
        lay.addWidget(
            QLabel(
                f"<b>{counts['add']}</b> new terms, <b>{counts['update']}</b> updates, "
                f"<b>{counts['conflict']}</b> conflicts with your glossary. "
                "Conflicts with confirmed terms are unticked by default."
            )
        )
        self.table = QTableWidget(len(changes), len(self.HEAD))
        self.table.setHorizontalHeaderLabels(self.HEAD)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        colors = {"add": QColor("#27ae60"), "update": QColor("#2f80ed"), "conflict": QColor("#c0392b")}
        for r, c in enumerate(changes):
            p = c.proposal
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            chk.setCheckState(Qt.Checked if c.accept else Qt.Unchecked)
            self.table.setItem(r, 0, chk)
            act = QTableWidgetItem(c.action)
            act.setForeground(colors[c.action])
            act.setToolTip(c.detail)
            values = [p.zh, p.en, (c.existing.en if c.existing else ""), p.category, ", ".join(p.aliases), p.gender, p.notes]
            self.table.setItem(r, 1, act)
            for col, v in enumerate(values, start=2):
                item = QTableWidgetItem(v)
                if col != 3:
                    item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                if col == 4 and c.detail:
                    item.setToolTip(c.detail)
                self.table.setItem(r, col, item)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.Interactive)
        self.table.resizeColumnsToContents()
        hh.setStretchLastSection(True)
        lay.addWidget(self.table, 1)

        row = QHBoxLayout()
        for text, fn in [
            ("Tick all", lambda: self._tick(lambda c: True)),
            ("Untick all", lambda: self._tick(lambda c: False)),
            ("Only new terms", lambda: self._tick(lambda c: c.action == "add")),
        ]:
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)

        self.style_list = QListWidget()
        if style_observations:
            lay.addWidget(QLabel("Style observations — ticked items will be appended to the style guide:"))
            for o in style_observations:
                it = QListWidgetItem(o)
                it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
                it.setCheckState(Qt.Unchecked)
                self.style_list.addItem(it)
            self.style_list.setMaximumHeight(140)
            lay.addWidget(self.style_list)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Apply selected")
        buttons.accepted.connect(self._ok)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        self.resize(1100, 650)

    def _tick(self, pred: Callable[[ProposedChange], bool]) -> None:
        for r, c in enumerate(self.changes):
            self.table.item(r, 0).setCheckState(Qt.Checked if pred(c) else Qt.Unchecked)

    def _ok(self) -> None:
        for r, c in enumerate(self.changes):
            c.accept = self.table.item(r, 0).checkState() == Qt.Checked
            new_en = self.table.item(r, 3).text().strip()
            if new_en:
                c.proposal.en = new_en
        self.accept()

    def selected_style_observations(self) -> list[str]:
        return [
            self.style_list.item(i).text()
            for i in range(self.style_list.count())
            if self.style_list.item(i).checkState() == Qt.Checked
        ]


class SettingsDialog(QDialog):
    def __init__(self, parent: QWidget, settings: Settings, test_fn: Callable[[Settings], None],
                 voices: dict[str, list[str]] | None = None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.settings = settings
        self.test_fn = test_fn
        form = QFormLayout()

        self.claude_path = QLineEdit(settings.claude_path)
        self.claude_path.setPlaceholderText("Auto-detect")
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        path_row = QHBoxLayout()
        path_row.addWidget(self.claude_path, 1)
        path_row.addWidget(browse)
        form.addRow("Claude Code executable", path_row)

        self.model = QComboBox()
        self.model.setEditable(True)
        self.model.addItems(["", "opus", "sonnet", "haiku", "claude-opus-5-5", "claude-sonnet-5-5"])
        self.model.setCurrentText(settings.model)
        self.model.setToolTip("Empty = your Claude Code default model")
        form.addRow("Model", self.model)

        efforts = ["", "low", "medium", "high", "xhigh", "max"]
        self.effort = QComboBox()
        self.effort.addItems(efforts)
        self.effort.setCurrentText(settings.effort)
        self.effort.setToolTip("Higher effort = better quality on hard passages, slower, uses more of your plan")
        form.addRow("Effort (translate & sync)", self.effort)
        self.summary_effort = QComboBox()
        self.summary_effort.addItems(efforts)
        self.summary_effort.setCurrentText(settings.summary_effort)
        form.addRow("Effort (story summary)", self.summary_effort)
        self.explain_effort = QComboBox()
        self.explain_effort.addItems(efforts)
        self.explain_effort.setCurrentText(settings.explain_effort)
        self.explain_effort.setToolTip("Used by Dictionary → Explain word (Ctrl+E). Low is fast and usually enough.")
        form.addRow("Effort (explain word)", self.explain_effort)

        self.chunk = QSpinBox()
        self.chunk.setRange(500, 20000)
        self.chunk.setSingleStep(500)
        self.chunk.setValue(settings.chunk_chars)
        self.chunk.setSuffix(" Chinese chars")
        form.addRow("Chunk size", self.chunk)

        self.context = QSpinBox()
        self.context.setRange(0, 10)
        self.context.setValue(settings.context_paragraphs)
        form.addRow("Preceding context paragraphs", self.context)

        self.timeout = QSpinBox()
        self.timeout.setRange(60, 7200)
        self.timeout.setValue(settings.timeout_seconds)
        self.timeout.setSuffix(" s")
        form.addRow("Timeout per call", self.timeout)

        self.update_summary = QCheckBox("Update the story summary after each translation")
        self.update_summary.setChecked(settings.update_summary)
        form.addRow("", self.update_summary)

        self.font_size = QSpinBox()
        self.font_size.setRange(8, 28)
        self.font_size.setValue(settings.font_size)
        form.addRow("Editor font size", self.font_size)

        voices = voices or {}
        self.voice_zh = QComboBox()
        self.voice_en = QComboBox()
        for combo, lang, current in ((self.voice_zh, "zh", settings.tts_voice_zh), (self.voice_en, "en", settings.tts_voice_en)):
            combo.addItem("Automatic", "")
            for name in voices.get(lang, []):
                combo.addItem(name, name)
            i = combo.findData(current)
            combo.setCurrentIndex(max(i, 0))
        if not voices.get("zh"):
            self.voice_zh.setToolTip("No Chinese voice installed. Add one in Windows Settings → Time & language → "
                                     "Language & region (Chinese → Text-to-speech).")
        form.addRow("Read-aloud voice (Chinese)", self.voice_zh)
        form.addRow("Read-aloud voice (English)", self.voice_en)
        self.tts_rate = QSpinBox()
        self.tts_rate.setRange(-10, 10)
        self.tts_rate.setValue(settings.tts_rate)
        self.tts_rate.setToolTip("-10 = slowest, 0 = normal, 10 = fastest")
        form.addRow("Read-aloud speed", self.tts_rate)

        self.patterns = QPlainTextEdit("\n".join(settings.cleanup_patterns))
        self.patterns.setToolTip("One regular expression per line. Matches are removed by Clean Raw.")
        self.patterns.setMaximumHeight(150)
        form.addRow("Clean Raw patterns", self.patterns)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        test = QPushButton("Test Claude Code connection")
        test.clicked.connect(lambda: self.test_fn(self.collect()))

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(test)
        lay.addWidget(self.status)
        lay.addWidget(buttons)
        self.resize(560, 0)

    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Claude Code executable", "", "Executables (*.exe *.cmd);;All files (*)")
        if path:
            self.claude_path.setText(path)

    def collect(self) -> Settings:
        s = Settings(**{k: getattr(self.settings, k) for k in Settings.__dataclass_fields__})
        s.claude_path = self.claude_path.text().strip()
        s.model = self.model.currentText().strip()
        s.effort = self.effort.currentText()
        s.summary_effort = self.summary_effort.currentText()
        s.explain_effort = self.explain_effort.currentText()
        s.chunk_chars = self.chunk.value()
        s.context_paragraphs = self.context.value()
        s.timeout_seconds = self.timeout.value()
        s.update_summary = self.update_summary.isChecked()
        s.font_size = self.font_size.value()
        s.tts_voice_zh = self.voice_zh.currentData() or ""
        s.tts_voice_en = self.voice_en.currentData() or ""
        s.tts_rate = self.tts_rate.value()
        s.cleanup_patterns = [p for p in self.patterns.toPlainText().splitlines() if p.strip()]
        return s


class HistoryDialog(QDialog):
    def __init__(self, parent: QWidget, title: str, items: list[HistoryItem], read: Callable[[HistoryItem], str]):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.items = items
        self.read = read
        self.selected: HistoryItem | None = None
        self.list = QListWidget()
        for it in items:
            self.list.addItem(f"{it.timestamp}  ·  {it.label}")
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        split = QSplitter()
        split.addWidget(self.list)
        split.addWidget(self.preview)
        split.setSizes([260, 640])
        self.list.currentRowChanged.connect(self._show)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Restore this version")
        buttons.accepted.connect(self._ok)
        buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        if not items:
            lay.addWidget(QLabel("No earlier versions yet."))
        lay.addWidget(split, 1)
        lay.addWidget(buttons)
        self.resize(950, 600)
        if items:
            self.list.setCurrentRow(0)

    def _show(self, row: int) -> None:
        if 0 <= row < len(self.items):
            self.preview.setPlainText(self.read(self.items[row]))

    def _ok(self) -> None:
        row = self.list.currentRow()
        if 0 <= row < len(self.items):
            self.selected = self.items[row]
            self.accept()
