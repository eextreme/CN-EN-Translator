"""Main window: Frames A/B/C, chapters, warnings, style guide, summary and all AI actions."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Callable

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QColor, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDockWidget,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTableView,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from ..claude_bridge import ClaudeBridge
from ..dictionary import CEDICT_FILENAME, Dictionary, LookupResult, WordList, download_cedict, sentence_at
from ..glossary import Glossary, apply_changes, check_consistency, diff_proposals
from ..models import CATEGORIES, DEFAULT_NAMING, NAMING_MODES, GlossaryEntry, Warning
from ..project import Chapter, Project
from ..services import UNTRANSLATED_MARK, TotalStats, TranslateOutcome, Translator, explain_word
from ..settings import Settings, config_dir
from ..text_utils import clean_raw, join_paragraphs, replace_paragraphs, split_paragraphs, to_simplified
from . import workers
from .dictionary_panel import DictionaryPanel, panel_html, tooltip_html
from .dialogs import HistoryDialog, SettingsDialog, SyncReviewDialog, TermDialog
from .editors import TextFrame
from .speech import Speaker
from .glossary_model import COL, GlossaryFilter, GlossaryModel, category_delegate, gender_delegate

HELP_TEXT = """<h3>Workflow</h3>
<ol>
<li><b>Create or open a novel project</b> (File menu). Each novel has its own glossary, style guide and story summary.</li>
<li><b>Learn from an existing translation:</b> paste a Chinese chapter into <b>Frame A</b> and its English translation into
<b>Frame B</b>, then press <b>Sync</b>. Review the proposed terms and apply them to the glossary (<b>Frame C</b>).</li>
<li><b>Translate:</b> create a new chapter, paste the Chinese into Frame A and press <b>Translate</b>. Only the glossary terms
that appear in the chapter are sent, together with the style guide and story summary.</li>
<li><b>Review warnings:</b> terms Claude was unsure about and glossary terms missing from the output are listed in the
<b>Warnings</b> tab and highlighted in the text. Add them to the glossary with one click.</li>
<li><b>Refine:</b> edit the glossary and use <b>Fix flagged</b>, <b>Retranslate selection</b> (with an optional instruction) or
translate again. You can also edit Frame B by hand and press <b>Sync</b> so the glossary learns from your edits.</li>
</ol>
<p><b>Direct vs. indirect names:</b> in <b>Style &amp; naming</b>, choose per category whether new unique names
are translated by meaning (蓝月 → Blue Moon), in pinyin (南岳 → Nan Yue) or pinyin + translated suffix (南岳山 → Nanyue Mountain).
Each uncertain-term warning also shows the other rendering; <b>Use alternative</b> switches to it in the glossary and in Frame B.</p>
<p><b>Highlights:</b> blue underline = glossary term (dotted = unconfirmed), orange = uncertain term, red = glossary term missing
from the translation. Hover a term for its glossary entry. Clicking a paragraph highlights the matching paragraph in the other
frame when both have the same number of paragraphs.</p>
<p><b>Read aloud (Ctrl+R):</b> right-click selected text or a paragraph and choose <b>Read aloud</b>. Chinese and English
use matching voices (set them in Settings). Press Ctrl+R again to stop.</p>
<p><b>Read aloud on long hover:</b> turn it on in the Dictionary menu, then rest the mouse on a character, word or
selected phrase (in either frame) for 5 seconds to hear it. The delay can be changed in Settings.</p>
<p><b>Pop-up dictionary:</b> hover a Chinese word in Frame A for its pinyin and meaning (CC-CEDICT). The Dictionary
panel on the right shows the full entry, and the word is read aloud once when its pop-up appears (turn this off with
Dictionary → Speak word when pop-up appears). <b>Ctrl+E</b> asks Claude to explain the word (or your selection) in its sentence;
answers are saved. <b>Ctrl+D</b> stars a word for your word list, and words you look up more than once appear there too.</p>
<p>Earlier versions of translations and the glossary are kept automatically (Chapter → History, Glossary → History).</p>"""


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = Settings.load()
        self.project: Project | None = None
        self.chapter: Chapter | None = None
        self.worker: workers.Worker | None = None
        self.ai_actions: list[QAction] = []
        self._partial: dict[int, str] = {}
        self.speaker = Speaker(self)
        self._speech_frame: TextFrame | None = None
        self._speech_offset = 0
        # pop-up dictionary
        self.dictionary: Dictionary | None = None
        self.wordlist = WordList(config_dir() / "wordlist.json")
        self._dict_worker: workers.Worker | None = None
        self._explain_worker: workers.Worker | None = None
        self._explaining: str = ""
        self._hover_seen: set[str] = set()
        self._dict_current: dict | None = None  # word, result, glossary, sentence of the word shown in the panel

        self.setWindowTitle(f"LN Translator {__version__}")
        self.resize(1500, 950)
        self._build_ui()
        self._build_actions()
        self._apply_font()
        self._apply_speech_settings()
        self._set_project_enabled(False)

        self._glossary_timer = self._debounce(self._on_glossary_changed_debounced, 250)
        self._text_timer = self._debounce(self._on_text_changed_debounced, 300)
        self._notes_timer = self._debounce(self._save_notes, 800)
        self._wordlist_timer = self._debounce(self._save_wordlist, 2000)
        self._load_dictionary()

        if self.settings.recent_projects and Project.is_project(Path(self.settings.recent_projects[0])):
            self.open_project(Path(self.settings.recent_projects[0]))
        else:
            self.statusBar().showMessage("Create or open a novel project to begin (File menu).")

    # ================================================================== UI construction
    def _debounce(self, fn: Callable[[], None], ms: int) -> QTimer:
        t = QTimer(self)
        t.setSingleShot(True)
        t.setInterval(ms)
        t.timeout.connect(fn)
        return t

    def _build_ui(self) -> None:
        # --- Frames A and B
        self.frame_a = TextFrame("zh", "Frame A — paste the Chinese chapter here")
        self.frame_b = TextFrame("en", "Frame B — paste an existing English translation (then Sync), or press Translate")
        self.label_a = QLabel("<b>Frame A</b> · Chinese raw")
        self.label_b = QLabel("<b>Frame B</b> · English")
        self.align_label = QLabel("")
        box_a, box_b = QWidget(), QWidget()
        la = QVBoxLayout(box_a)
        la.setContentsMargins(0, 0, 0, 0)
        la.addWidget(self.label_a)
        la.addWidget(self.frame_a)
        lb = QVBoxLayout(box_b)
        lb.setContentsMargins(0, 0, 0, 0)
        hb = QHBoxLayout()
        hb.addWidget(self.label_b)
        hb.addStretch(1)
        hb.addWidget(self.align_label)
        lb.addLayout(hb)
        lb.addWidget(self.frame_b)
        self.text_split = QSplitter(Qt.Horizontal)
        self.text_split.addWidget(box_a)
        self.text_split.addWidget(box_b)

        for f in (self.frame_a, self.frame_b):
            f.textChanged.connect(lambda: self._text_timer.start())
            f.addTermRequested.connect(self.add_term_from_selection)
            f.retranslateRequested.connect(lambda i: self.retranslate([i]))
            f.findInGlossaryRequested.connect(self.find_in_glossary)
            f.readAloudRequested.connect(lambda f=f: self.read_aloud(f))
            f.stopReadingRequested.connect(self.stop_reading)
            f.is_speaking = self.speaker.is_speaking
            f.hoverSpeakRequested.connect(self.speak_hovered)
        self.speaker.speakingChanged.connect(self._on_speaking_changed)
        self.speaker.wordSpoken.connect(self._on_word_spoken)
        self.speaker.error.connect(lambda msg: QMessageBox.information(self, "Read aloud", msg))
        self.frame_a.hover_lookup = self._hover_lookup
        self.frame_a.word_at = self._word_at
        self.frame_a.speak_unit_at = self._speak_unit_at
        self.frame_a.lookupShown.connect(self._on_lookup_shown)
        self.frame_a.explainRequested.connect(lambda: self.explain_word(source="cursor"))
        self.frame_a.starRequested.connect(lambda: self.star_word(source="cursor"))
        self.frame_a.paragraphActivated.connect(lambda i: self._link(self.frame_b, i))
        self.frame_b.paragraphActivated.connect(lambda i: self._link(self.frame_a, i))

        # --- Bottom tabs: glossary (Frame C), warnings, style, summary, log
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_glossary_tab(), "Frame C · Glossary")
        self.tabs.addTab(self._build_warnings_tab(), "Warnings")
        self.style_edit = QPlainTextEdit()
        self.style_edit.setPlaceholderText("Per-novel style guide, sent with every translation.")
        self.style_edit.textChanged.connect(lambda: self._notes_timer.start())
        self.tabs.addTab(self._build_style_tab(), "Style & naming")
        self.tabs.addTab(self._build_summary_tab(), "Story summary")
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.tabs.addTab(self.log, "Log")

        self.main_split = QSplitter(Qt.Vertical)
        self.main_split.addWidget(self.text_split)
        self.main_split.addWidget(self.tabs)
        self.main_split.setSizes([560, 360])
        self.setCentralWidget(self.main_split)

        # --- Chapter dock
        dock_w = QWidget()
        dl = QVBoxLayout(dock_w)
        self.chapter_list = QListWidget()
        self.chapter_list.currentItemChanged.connect(self._on_chapter_selected)
        self.title_edit = QLineEdit()
        self.title_edit.setPlaceholderText("Chapter title")
        self.title_edit.editingFinished.connect(self._on_title_changed)
        row = QHBoxLayout()
        b_new = QPushButton("New")
        b_new.clicked.connect(self.new_chapter)
        b_del = QPushButton("Delete")
        b_del.clicked.connect(self.delete_chapter)
        row.addWidget(b_new)
        row.addWidget(b_del)
        dl.addWidget(self.chapter_list, 1)
        dl.addWidget(QLabel("Title"))
        dl.addWidget(self.title_edit)
        dl.addLayout(row)
        self.chapter_dock = QDockWidget("Chapters", self)
        self.chapter_dock.setObjectName("chapters")
        self.chapter_dock.setWidget(dock_w)
        self.chapter_dock.setFeatures(QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetFloatable)
        self.addDockWidget(Qt.LeftDockWidgetArea, self.chapter_dock)
        self.resizeDocks([self.chapter_dock], [190], Qt.Horizontal)

        # --- Dictionary dock
        self.dict_panel = DictionaryPanel(self.wordlist)
        self.dict_panel.speakRequested.connect(self.speak_current_word)
        self.dict_panel.explainRequested.connect(lambda: self.explain_word(source="panel"))
        self.dict_panel.starRequested.connect(lambda: self.star_word(source="panel"))
        self.dict_panel.downloadRequested.connect(self.download_dictionary)
        self.dict_panel.wordActivated.connect(self._show_list_word)
        self.dict_dock = QDockWidget("Dictionary", self)
        self.dict_dock.setObjectName("dictionary")
        self.dict_dock.setWidget(self.dict_panel)
        self.addDockWidget(Qt.RightDockWidgetArea, self.dict_dock)
        self.resizeDocks([self.dict_dock], [340], Qt.Horizontal)

        # --- Status bar
        self.busy = QProgressBar()
        self.busy.setRange(0, 0)
        self.busy.setMaximumWidth(140)
        self.busy.hide()
        self.usage_label = QLabel("")
        self.statusBar().addPermanentWidget(self.usage_label)
        self.statusBar().addPermanentWidget(self.busy)

    def _build_glossary_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        bar = QHBoxLayout()
        self.g_search = QLineEdit()
        self.g_search.setPlaceholderText("Search Chinese, English, aliases, notes…")
        self.g_search.setClearButtonEnabled(True)
        self.g_category = QComboBox()
        self.g_category.addItems(["All categories", *CATEGORIES])
        self.g_unconfirmed = QCheckBox("Unconfirmed only")
        self.g_in_chapter = QCheckBox("In this chapter")
        self.g_count = QLabel("")
        bar.addWidget(self.g_search, 1)
        bar.addWidget(self.g_category)
        bar.addWidget(self.g_unconfirmed)
        bar.addWidget(self.g_in_chapter)
        bar.addWidget(self.g_count)
        for text, fn in [
            ("Add", self.add_term),
            ("Delete", self.delete_terms),
            ("Confirm selected", self.confirm_terms),
            ("Import…", self.import_glossary),
            ("Export…", self.export_glossary),
        ]:
            b = QPushButton(text)
            b.clicked.connect(fn)
            bar.addWidget(b)
        lay.addLayout(bar)

        self.g_model = GlossaryModel(Glossary(), self._on_glossary_changed)
        self.g_proxy = GlossaryFilter()
        self.g_proxy.setSourceModel(self.g_model)
        self.g_table = QTableView()
        self.g_table.setModel(self.g_proxy)
        self.g_table.setSortingEnabled(True)
        self.g_table.sortByColumn(COL["zh"], Qt.AscendingOrder)
        self.g_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.g_table.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed | QAbstractItemView.AnyKeyPressed)
        self.g_table.setItemDelegateForColumn(COL["category"], category_delegate(self.g_table))
        self.g_table.setItemDelegateForColumn(COL["gender"], gender_delegate(self.g_table))
        self.g_table.verticalHeader().setVisible(False)
        hh = self.g_table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.Interactive)
        hh.setStretchLastSection(True)
        for key, width in [("confirmed", 30), ("zh", 130), ("en", 200), ("category", 100), ("aliases", 160), ("gender", 70), ("notes", 300)]:
            self.g_table.setColumnWidth(COL[key], width)
        lay.addWidget(self.g_table, 1)

        refilter = lambda *_: self._refilter_glossary()  # noqa: E731
        self.g_search.textChanged.connect(refilter)
        self.g_category.currentIndexChanged.connect(refilter)
        self.g_unconfirmed.toggled.connect(refilter)
        self.g_in_chapter.toggled.connect(refilter)
        return w

    def _build_style_tab(self) -> QWidget:
        """Style guide text plus per-category choice of direct (meaning) vs. indirect (pinyin) rendering."""
        panel = QWidget()
        form = QFormLayout(panel)
        intro = QLabel(
            "<b>Rendering of new unique names</b><br>Meaning: 蓝月 → Blue Moon · Pinyin: 南岳 → Nan Yue · "
            "Pinyin + suffix: 南岳山 → Nanyue Mountain.<br>Glossary entries always win; this applies to terms "
            "Claude hasn't seen yet."
        )
        intro.setWordWrap(True)
        form.addRow(intro)
        self.naming_boxes: dict[str, QComboBox] = {}
        for cat in DEFAULT_NAMING:
            box = QComboBox()
            for mode, (label, desc) in NAMING_MODES.items():
                box.addItem(label, mode)
                box.setItemData(box.count() - 1, desc, Qt.ToolTipRole)
            box.currentIndexChanged.connect(self._on_naming_changed)
            self.naming_boxes[cat] = box
            form.addRow(cat.capitalize(), box)
        presets = QHBoxLayout()
        for label, mode in [("All meaning", "meaning"), ("All pinyin", "pinyin"), ("Defaults", None)]:
            b = QPushButton(label)
            b.clicked.connect(lambda checked=False, m=mode: self._set_naming_all(m))
            presets.addWidget(b)
        form.addRow(presets)
        split = QSplitter(Qt.Horizontal)
        split.addWidget(self.style_edit)
        split.addWidget(panel)
        split.setSizes([700, 420])
        return split

    _loading_naming = False

    def _load_naming(self) -> None:
        self._loading_naming = True
        naming = self.project.naming if self.project else DEFAULT_NAMING
        for cat, box in self.naming_boxes.items():
            box.setCurrentIndex(max(0, box.findData(naming.get(cat, "auto"))))
        self._loading_naming = False

    def _set_naming_all(self, mode: str | None) -> None:
        self._loading_naming = True
        for cat, box in self.naming_boxes.items():
            box.setCurrentIndex(box.findData(mode or DEFAULT_NAMING[cat]))
        self._loading_naming = False
        self._on_naming_changed()

    def _on_naming_changed(self, *_args) -> None:
        if self.project and not self._loading_naming:
            self.project.naming = {cat: box.currentData() for cat, box in self.naming_boxes.items()}

    def _build_warnings_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        bar = QHBoxLayout()
        for text, fn, tip in [
            ("Add to glossary…", self.warning_to_glossary, "Open the selected warning as a new glossary term"),
            ("Accept suggestion", self.accept_warning_suggestion, "Add the suggested English to the glossary as-is"),
            ("Use alternative", lambda: self.accept_warning_suggestion(use_alternative=True),
             "Add the alternative rendering (pinyin ↔ meaning) to the glossary"),
            ("Dismiss", self.dismiss_warnings, "Remove the selected warnings"),
            ("Fix flagged paragraphs", self.fix_flagged, "Retranslate paragraphs with missing glossary terms or untranslated text"),
            ("Re-check consistency", self.check_consistency, "Check that glossary terms are used in the translation"),
        ]:
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(lambda checked=False, fn=fn: fn())
            bar.addWidget(b)
        bar.addStretch(1)
        lay.addLayout(bar)
        self.w_table = QTableWidget(0, 7)
        self.w_table.setHorizontalHeaderLabels(
            ["Type", "Chinese", "Suggested English", "Alternative", "Category", "Paragraph", "Reason"]
        )
        self.w_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.w_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.w_table.verticalHeader().setVisible(False)
        self.w_table.horizontalHeader().setStretchLastSection(True)
        for i, width in enumerate([90, 120, 180, 180, 100, 70]):
            self.w_table.setColumnWidth(i, width)
        self.w_table.cellDoubleClicked.connect(self._jump_to_warning)
        lay.addWidget(self.w_table, 1)
        return w

    def _build_summary_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        self.summary_edit = QPlainTextEdit()
        self.summary_edit.setPlaceholderText(
            "Rolling 'story so far' — sent with every translation to resolve pronouns and references. "
            "Updated automatically after each translation (see Settings), and you can edit it freely."
        )
        self.summary_edit.textChanged.connect(lambda: self._notes_timer.start())
        bar = QHBoxLayout()
        b = QPushButton("Update from current chapter")
        b.clicked.connect(lambda: self.update_summary(manual=True))
        self.ai_buttons = [b]
        bar.addWidget(b)
        bar.addStretch(1)
        lay.addLayout(bar)
        lay.addWidget(self.summary_edit, 1)
        return w

    def _build_actions(self) -> None:
        mb = self.menuBar()
        tb = QToolBar("Main")
        tb.setObjectName("main_toolbar")
        tb.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.addToolBar(tb)

        def act(text, fn, shortcut=None, tip=None, ai=False, toolbar=False, menu=None):
            a = QAction(text, self)
            a.triggered.connect(lambda checked=False: fn())
            if shortcut:
                a.setShortcut(QKeySequence(shortcut))
            if tip:
                a.setToolTip(tip)
                a.setStatusTip(tip)
            if ai:
                self.ai_actions.append(a)
            if toolbar:
                tb.addAction(a)
            if menu is not None:
                menu.addAction(a)
            return a

        m_file = mb.addMenu("&File")
        act("New project…", self.new_project, "Ctrl+Shift+N", menu=m_file)
        act("Open project…", self.open_project_dialog, "Ctrl+O", menu=m_file)
        self.recent_menu = m_file.addMenu("Open recent")
        self._refresh_recent()
        m_file.addSeparator()
        self.a_save = act("Save", self.save_all, "Ctrl+S", "Save chapter, glossary, style guide and summary", toolbar=True, menu=m_file)
        self.a_export = act("Export all translations to .txt…", self.export_translations, menu=m_file)
        m_file.addSeparator()
        act("Settings…", self.open_settings, "Ctrl+,", menu=m_file)
        act("Quit", self.close, "Ctrl+Q", menu=m_file)

        m_ch = mb.addMenu("&Chapter")
        self.a_new_ch = act("New chapter", self.new_chapter, "Ctrl+N", menu=m_ch)
        self.a_ren_ch = act("Rename chapter…", self.rename_chapter, menu=m_ch)
        self.a_del_ch = act("Delete chapter", self.delete_chapter, menu=m_ch)
        m_ch.addSeparator()
        self.a_load_raw = act("Load Chinese from file…", lambda: self.load_text_file(self.frame_a), menu=m_ch)
        self.a_load_en = act("Load English from file…", lambda: self.load_text_file(self.frame_b), menu=m_ch)
        tb.addSeparator()
        self.a_clean = act("Clean raw", self.clean_raw_text, tip="Remove website watermarks/ads and tidy whitespace in Frame A",
                           toolbar=True, menu=m_ch)
        self.a_simpl = act("Traditional → Simplified", self.convert_simplified, menu=m_ch)
        m_ch.addSeparator()
        self.a_hist = act("Translation history…", self.chapter_history, "Ctrl+H", menu=m_ch)
        m_ch.addSeparator()
        self.a_read = act("Read aloud", self.toggle_read_aloud, "Ctrl+R",
                          "Read the selected text (or the current paragraph) aloud; press again to stop", menu=m_ch)

        m_g = mb.addMenu("&Glossary")
        self.a_add_term = act("Add term…", self.add_term, "Ctrl+G", menu=m_g)
        act("Import CSV/JSON…", self.import_glossary, menu=m_g)
        act("Export CSV/JSON…", self.export_glossary, menu=m_g)
        m_g.addSeparator()
        act("Glossary history…", self.glossary_history, menu=m_g)

        m_d = mb.addMenu("&Dictionary")
        self.a_hover = act("Look up words on hover", self._toggle_hover, menu=m_d)
        self.a_hover.setCheckable(True)
        self.a_hover.setChecked(self.settings.dict_hover)
        act("Explain word", lambda: self.explain_word(source="auto"), "Ctrl+E",
            "Ask Claude what the hovered word (or the selection) means in its sentence", menu=m_d)
        act("Star word", lambda: self.star_word(source="auto"), "Ctrl+D",
            "Add the hovered word (or the selection) to your word list", menu=m_d)
        act("Speak word", self.speak_current_word, "Ctrl+Shift+R", menu=m_d)
        self.a_hover_speak = act("Read aloud on long hover", self._toggle_hover_speak, menu=m_d)
        self.a_hover_speak.setCheckable(True)
        self.a_hover_speak.setChecked(self.settings.hover_speak)
        self.a_auto_speak = act("Speak word when pop-up appears", self._toggle_auto_speak, menu=m_d)
        self.a_auto_speak.setCheckable(True)
        self.a_auto_speak.setChecked(self.settings.dict_auto_speak)
        m_d.addSeparator()
        m_d.addAction(self.dict_dock.toggleViewAction())
        act("Download / update CC-CEDICT…", self.download_dictionary, menu=m_d)

        m_ai = mb.addMenu("&AI")
        tb.addSeparator()
        act("Sync", self.sync, "F5", "Learn terminology from the translation in Frame B", ai=True, toolbar=True, menu=m_ai)
        act("Translate", self.translate, "F6", "Translate Frame A into Frame B using the glossary", ai=True, toolbar=True, menu=m_ai)
        act("Retranslate selection", self.retranslate_selection, "F7",
            "Retranslate the selected paragraph(s), optionally with an instruction", ai=True, toolbar=True, menu=m_ai)
        act("Fix flagged", self.fix_flagged, "F8", "Retranslate paragraphs with missing glossary terms",
            ai=True, toolbar=True, menu=m_ai)
        act("Check consistency", self.check_consistency, "F9", "Check glossary usage without AI", toolbar=True, menu=m_ai)
        act("Update story summary", lambda: self.update_summary(manual=True), ai=True, menu=m_ai)
        m_ai.addSeparator()
        self.a_cancel = act("Cancel", self.cancel_ai, "Ctrl+Shift+X", "Stop the running AI operation", toolbar=True, menu=m_ai)
        self.a_cancel.setEnabled(False)
        act("Test Claude Code connection", lambda: self.test_connection(self.settings), menu=m_ai)

        tb.addSeparator()
        tb.addAction(self.a_hist)
        tb.addAction(self.a_read)

        m_help = mb.addMenu("&Help")
        act("How to use", lambda: QMessageBox.about(self, "How to use", HELP_TEXT), "F1", menu=m_help)

    def _apply_speech_settings(self) -> None:
        self.speaker.voice_prefs = {"zh": self.settings.tts_voice_zh, "en": self.settings.tts_voice_en}
        self.speaker.rate = self.settings.tts_rate / 10
        for f in (self.frame_a, self.frame_b):
            f.set_hover_speak(self.settings.hover_speak, self.settings.hover_speak_seconds)

    # ----- read aloud -------------------------------------------------------
    def toggle_read_aloud(self) -> None:
        if self.speaker.is_speaking():
            self.stop_reading()
            return
        frame = self.frame_a if self.frame_a.hasFocus() else self.frame_b if self.frame_b.hasFocus() else None
        if frame is None:  # e.g. toolbar click: use whichever frame has a selection, else Frame B
            frame = self.frame_a if self.frame_a.textCursor().hasSelection() else self.frame_b
        self.read_aloud(frame)

    def read_aloud(self, frame: TextFrame) -> None:
        offset, text = frame.text_to_read()
        if not text.strip():
            self.statusBar().showMessage("Read aloud: select some text or place the cursor in a paragraph.", 4000)
            return
        self._clear_speech_highlight()
        self._speech_frame, self._speech_offset = frame, offset
        if not self.speaker.speak(text):
            self._speech_frame = None

    def stop_reading(self) -> None:
        self.speaker.stop()
        self._clear_speech_highlight()

    def _clear_speech_highlight(self) -> None:
        if self._speech_frame is not None:
            self._speech_frame.set_speech_range(None)

    def _on_word_spoken(self, start: int, length: int) -> None:
        if self._speech_frame is not None:
            self._speech_frame.set_speech_range(self._speech_offset + start, length)

    def _on_speaking_changed(self, speaking: bool) -> None:
        self.a_read.setText("Stop reading" if speaking else "Read aloud")
        if not speaking:
            self._clear_speech_highlight()

    def _apply_font(self) -> None:
        for f in (self.frame_a, self.frame_b):
            f.set_font_size(self.settings.font_size)

    def _refresh_recent(self) -> None:
        self.recent_menu.clear()
        for p in self.settings.recent_projects:
            a = self.recent_menu.addAction(p)
            a.triggered.connect(lambda checked=False, p=p: self.open_project(Path(p)))
        self.recent_menu.setEnabled(bool(self.settings.recent_projects))

    def _set_project_enabled(self, on: bool) -> None:
        for w in (self.frame_a, self.frame_b, self.tabs, self.chapter_dock):
            w.setEnabled(on)
        for a in [self.a_save, self.a_export, self.a_new_ch, self.a_ren_ch, self.a_del_ch, self.a_load_raw, self.a_load_en,
                  self.a_clean, self.a_simpl, self.a_hist, self.a_add_term, *self.ai_actions]:
            a.setEnabled(on and (a not in self.ai_actions or self.worker is None))

    # ================================================================== projects
    def new_project(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose an empty folder for the new novel project")
        if not folder:
            return
        name, ok = QInputDialog.getText(self, "New project", "Novel name:", text=Path(folder).name)
        if not ok:
            return
        Project.create(Path(folder), name.strip() or Path(folder).name)
        self.open_project(Path(folder))

    def open_project_dialog(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Open novel project folder")
        if not folder:
            return
        if not Project.is_project(Path(folder)):
            if QMessageBox.question(self, "Not a project", "This folder is not a project yet. Create one here?") != QMessageBox.Yes:
                return
            Project.create(Path(folder), Path(folder).name)
        self.open_project(Path(folder))

    def open_project(self, root: Path) -> None:
        if self.worker:
            QMessageBox.information(self, "Busy", "Wait for the current AI operation to finish or cancel it.")
            return
        self.save_all(quiet=True)
        try:
            self.project = Project(root)
        except (OSError, json.JSONDecodeError) as e:
            QMessageBox.critical(self, "Could not open project", str(e))
            return
        self.chapter = None
        self.settings.add_recent(str(root))
        self.settings.save()
        self._refresh_recent()
        self.setWindowTitle(f"{self.project.name} — LN Translator {__version__}")
        self.g_model.set_glossary(self.project.glossary)
        self._loading_notes = True
        self.style_edit.setPlainText(self.project.style)
        self.summary_edit.setPlainText(self.project.summary)
        self._loading_notes = False
        self._load_naming()
        self._set_project_enabled(True)
        self._reload_chapter_list()
        if self.chapter_list.count() == 0:
            self.new_chapter()
        else:
            self.chapter_list.setCurrentRow(self.chapter_list.count() - 1)
        self._refresh_glossary_views()
        self.log_line(f"Opened project “{self.project.name}” ({len(self.project.glossary.entries)} glossary terms).")

    def _reload_chapter_list(self, select: str | None = None) -> None:
        self.chapter_list.blockSignals(True)
        self.chapter_list.clear()
        for cid in self.project.chapter_ids():
            ch = self.project.load_chapter(cid)
            item = QListWidgetItem(ch.label)
            item.setData(Qt.UserRole, cid)
            done = bool(ch.translation.strip())
            item.setToolTip("Has translation" if done else "No translation yet")
            if not done:
                item.setForeground(QColor("gray"))
            self.chapter_list.addItem(item)
        self.chapter_list.blockSignals(False)
        if select:
            for i in range(self.chapter_list.count()):
                if self.chapter_list.item(i).data(Qt.UserRole) == select:
                    self.chapter_list.setCurrentRow(i)
                    break

    # ================================================================== chapters
    def _on_chapter_selected(self, current: QListWidgetItem | None, _previous=None) -> None:
        if current is None or self.project is None:
            return
        cid = current.data(Qt.UserRole)
        if self.chapter and self.chapter.id == cid:
            return
        if self.worker:
            QMessageBox.information(self, "Busy", "Wait for the current AI operation to finish or cancel it.")
            self._reload_chapter_list(select=self.chapter.id if self.chapter else None)
            return
        self.save_chapter(quiet=True)
        self.chapter = self.project.load_chapter(cid)
        self.stop_reading()
        self._hover_seen.clear()
        self.frame_a.load_text(self.chapter.raw)
        self.frame_b.load_text(self.chapter.translation)
        self.title_edit.setText(self.chapter.title)
        self._refresh_warnings()
        self._on_text_changed_debounced()

    def new_chapter(self) -> None:
        if not self.project:
            return
        self.save_chapter(quiet=True)
        ch = self.project.new_chapter()
        self._reload_chapter_list(select=ch.id)
        self.frame_a.setFocus()

    def rename_chapter(self) -> None:
        if not self.chapter:
            return
        new_id, ok = QInputDialog.getText(self, "Rename chapter", "Chapter id (used for sorting):", text=self.chapter.id)
        if not ok or not new_id.strip() or new_id.strip() == self.chapter.id:
            return
        self.save_chapter(quiet=True)
        try:
            cid = self.project.rename_chapter(self.chapter.id, new_id.strip())
        except (ValueError, OSError) as e:
            QMessageBox.warning(self, "Rename failed", str(e))
            return
        self.chapter = None
        self._reload_chapter_list(select=cid)

    def delete_chapter(self) -> None:
        if not self.chapter:
            return
        if QMessageBox.question(
            self, "Delete chapter", f"Delete chapter {self.chapter.label}?\nA copy is kept in the project's history/deleted folder."
        ) != QMessageBox.Yes:
            return
        self.project.delete_chapter(self.chapter.id)
        self.chapter = None
        self._reload_chapter_list()
        if self.chapter_list.count():
            self.chapter_list.setCurrentRow(self.chapter_list.count() - 1)
        else:
            self.new_chapter()

    def _on_title_changed(self) -> None:
        if self.chapter and self.title_edit.text().strip() != self.chapter.title:
            self.chapter.title = self.title_edit.text().strip()
            self.save_chapter(quiet=True)
            self._reload_chapter_list(select=self.chapter.id)

    def _sync_chapter_from_ui(self) -> None:
        if self.chapter:
            self.chapter.raw = self.frame_a.toPlainText()
            self.chapter.translation = self.frame_b.toPlainText()
            self.chapter.title = self.title_edit.text().strip()

    def save_chapter(self, quiet: bool = False, history_label: str | None = None) -> None:
        if not (self.project and self.chapter):
            return
        self._sync_chapter_from_ui()
        self.project.save_chapter(self.chapter, history_label)
        if not quiet:
            self.statusBar().showMessage("Saved.", 3000)

    def save_all(self, quiet: bool = False) -> None:
        if not self.project:
            return
        self.save_chapter(quiet=True, history_label=None if quiet else "manual-save")
        self.project.save_glossary()
        self._save_notes()
        if not quiet:
            self.statusBar().showMessage("Saved chapter, glossary, style guide and summary.", 3000)
            self._reload_chapter_list(select=self.chapter.id if self.chapter else None)

    _loading_notes = False

    def _save_notes(self) -> None:
        if self.project and not self._loading_notes:
            if self.style_edit.toPlainText() != self.project.style:
                self.project.style = self.style_edit.toPlainText()
            if self.summary_edit.toPlainText() != self.project.summary:
                self.project.summary = self.summary_edit.toPlainText()

    def load_text_file(self, frame: TextFrame) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Load text", "", "Text files (*.txt);;All files (*)")
        if not path:
            return
        data = Path(path).read_bytes()
        for enc in ("utf-8-sig", "gb18030", "big5", "utf-16"):
            try:
                text = data.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            QMessageBox.warning(self, "Unknown encoding", "Could not decode this file.")
            return
        frame.set_text_undoable(text)

    def export_translations(self) -> None:
        if not self.project:
            return
        self.save_chapter(quiet=True)
        path, _ = QFileDialog.getSaveFileName(self, "Export translations", f"{self.project.name}.txt", "Text (*.txt)")
        if path:
            n = self.project.export_translations(Path(path))
            self.statusBar().showMessage(f"Exported {n} translated chapters.", 5000)

    def clean_raw_text(self) -> None:
        text, changed = clean_raw(self.frame_a.toPlainText(), self.settings.cleanup_patterns)
        self.frame_a.set_text_undoable(text)
        self.statusBar().showMessage(f"Clean raw: {changed} line(s) changed or removed (Ctrl+Z to undo).", 6000)

    def convert_simplified(self) -> None:
        try:
            self.frame_a.set_text_undoable(to_simplified(self.frame_a.toPlainText()))
        except RuntimeError as e:
            QMessageBox.warning(self, "Not available", str(e))

    def chapter_history(self) -> None:
        if not self.chapter:
            return
        self.save_chapter(quiet=True)
        dlg = HistoryDialog(self, f"Translation history — {self.chapter.label}", self.project.chapter_history(self.chapter.id),
                            Project.read_history)
        if dlg.exec() and dlg.selected:
            self.frame_b.set_text_undoable(Project.read_history(dlg.selected))
            self.save_chapter(quiet=True, history_label="before-restore")
            self.check_consistency()

    # ================================================================== text / linking
    def _on_text_changed_debounced(self) -> None:
        a = split_paragraphs(self.frame_a.toPlainText())
        b = split_paragraphs(self.frame_b.toPlainText())
        self.label_a.setText(f"<b>Frame A</b> · Chinese raw · {len(a)} paragraphs · {sum(map(len, a)):,} chars")
        self.label_b.setText(f"<b>Frame B</b> · English · {len(b)} paragraphs · {sum(len(p.split()) for p in b):,} words")
        if a and b:
            if len(a) == len(b):
                self.align_label.setText("<span style='color:#27ae60'>✓ paragraphs aligned</span>")
                self.align_label.setToolTip("Clicking a paragraph highlights its partner; paragraph retranslation is available.")
            else:
                self.align_label.setText(f"<span style='color:#e67e22'>⚠ {len(a)} vs {len(b)} paragraphs</span>")
                self.align_label.setToolTip(
                    "Paragraph counts differ (normal for pasted reference translations). Sync still works; "
                    "paragraph linking and paragraph retranslation need a matching count."
                )
        else:
            self.align_label.setText("")
        if self.g_in_chapter.isChecked():
            self._refilter_glossary()

    def _aligned(self) -> bool:
        a = split_paragraphs(self.frame_a.toPlainText())
        return bool(a) and len(a) == len(split_paragraphs(self.frame_b.toPlainText()))

    def _link(self, other: TextFrame, index: int | None) -> None:
        if index is not None and self._aligned():
            other.show_linked_paragraph(index)
        else:
            other.show_linked_paragraph(None, move=False)

    def _selected_paragraphs(self) -> list[int]:
        frame = self.frame_b if self.frame_b.hasFocus() or not self.frame_a.hasFocus() else self.frame_a
        cur = frame.textCursor()
        doc = frame.document()
        start_block = doc.findBlock(cur.selectionStart())
        end_block = doc.findBlock(cur.selectionEnd())
        out = []
        b = start_block
        while b.isValid():
            idx = frame.paragraph_of_block(b)
            if idx is not None:
                out.append(idx)
            if b == end_block:
                break
            b = b.next()
        return out

    # ================================================================== glossary
    def _on_glossary_changed(self) -> None:
        self._glossary_timer.start()

    def _on_glossary_changed_debounced(self) -> None:
        if self.project:
            self.project.save_glossary()
        self._refresh_glossary_views()

    def _refresh_glossary_views(self) -> None:
        if not self.project:
            return
        warnings = self.chapter.warnings if self.chapter else []
        self.frame_a.highlighter.set_data(self.project.glossary, warnings)
        self.frame_b.highlighter.set_data(self.project.glossary, warnings)
        self._refilter_glossary()

    def _refilter_glossary(self) -> None:
        if not self.project:
            return
        cat = self.g_category.currentText()
        self.g_proxy.set_filters(self.g_search.text(), "" if cat.startswith("All") else cat, self.g_unconfirmed.isChecked())
        if self.g_in_chapter.isChecked():
            ids = {e.id for e in self.project.glossary.relevant_entries(self.frame_a.toPlainText())}
            self.g_proxy.in_chapter = ids
        else:
            self.g_proxy.in_chapter = None
        self.g_proxy.invalidateFilter()
        total = len(self.project.glossary.entries)
        confirmed = sum(1 for e in self.project.glossary.entries if e.confirmed)
        self.g_count.setText(f"{self.g_proxy.rowCount()} shown · {total} terms · {confirmed} confirmed")

    def find_in_glossary(self, text: str) -> None:
        self.tabs.setCurrentIndex(0)
        self.g_search.setText(text)

    def _selected_glossary_rows(self) -> list[int]:
        rows = {self.g_proxy.mapToSource(i).row() for i in self.g_table.selectionModel().selectedRows()}
        return sorted(rows)

    def _edit_or_add(self, entry: GlossaryEntry, title: str, alternatives: list[str] = ()) -> GlossaryEntry | None:
        existing = self.project.glossary.find_by_key(entry.zh) if entry.zh else None
        if existing:
            if QMessageBox.question(
                self, "Already in glossary", f"“{entry.zh}” is already in the glossary as “{existing.en}”. Edit that entry?"
            ) != QMessageBox.Yes:
                return None
            if entry.en and not existing.en:
                existing.en = entry.en
            dlg = TermDialog(self, existing, "Edit glossary term")
            if dlg.exec():
                self.project.glossary.invalidate()
                self.g_model.refresh()
                self._on_glossary_changed()
                return existing
            return None
        dlg = TermDialog(self, entry, title, alternatives)
        if dlg.exec():
            if self.project.glossary.find_by_key(entry.zh):
                QMessageBox.warning(self, "Duplicate", f"“{entry.zh}” is already in the glossary.")
                return None
            self.g_model.add_entry(entry)
            return entry
        return None

    def add_term(self) -> None:
        if self.project:
            self._edit_or_add(GlossaryEntry(zh="", en="", first_seen=self.chapter.id if self.chapter else ""), "Add glossary term")

    def add_term_from_selection(self, text: str, side: str) -> None:
        if not self.project:
            return
        cid = self.chapter.id if self.chapter else ""
        entry = GlossaryEntry(zh=text, en="", first_seen=cid) if side == "zh" else GlossaryEntry(zh="", en=text, first_seen=cid)
        if self._edit_or_add(entry, "Add glossary term"):
            self._drop_warnings_for(entry.zh)

    def delete_terms(self) -> None:
        rows = self._selected_glossary_rows()
        if not rows:
            return
        if QMessageBox.question(self, "Delete terms", f"Delete {len(rows)} glossary term(s)?") != QMessageBox.Yes:
            return
        self.project.snapshot_glossary("before-delete")
        self.g_model.remove_rows(rows)

    def confirm_terms(self) -> None:
        rows = self._selected_glossary_rows()
        for r in rows:
            self.g_model.entry(r).confirmed = True
        if rows:
            self.g_model.refresh()
            self._on_glossary_changed()

    def import_glossary(self) -> None:
        if not self.project:
            return
        path, _ = QFileDialog.getOpenFileName(self, "Import glossary", "", "Glossary (*.csv *.json);;All files (*)")
        if not path:
            return
        try:
            text = Path(path).read_text(encoding="utf-8-sig")
            incoming = Glossary.from_json(text).entries if path.lower().endswith(".json") else Glossary.parse_csv(text)
        except (OSError, ValueError, json.JSONDecodeError) as e:
            QMessageBox.warning(self, "Import failed", str(e))
            return
        if not incoming:
            QMessageBox.information(self, "Import", "No terms found. CSV files need at least 'zh' and 'en' columns.")
            return
        overwrite = QMessageBox.question(
            self, "Import", f"{len(incoming)} terms found. Overwrite existing terms with the same Chinese?",
            QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
        )
        if overwrite == QMessageBox.Cancel:
            return
        self.project.snapshot_glossary("before-import")
        added, updated = self.project.glossary.merge(incoming, overwrite == QMessageBox.Yes)
        self.g_model.refresh()
        self._on_glossary_changed()
        self.log_line(f"Imported glossary: {added} added, {updated} updated.")

    def export_glossary(self) -> None:
        if not self.project:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export glossary", f"{self.project.name}-glossary.csv", "CSV (*.csv);;JSON (*.json)")
        if not path:
            return
        g = self.project.glossary
        if path.lower().endswith(".json"):
            Path(path).write_text(g.to_json(), encoding="utf-8")
        else:
            Path(path).write_text(g.to_csv(), encoding="utf-8-sig")  # BOM so Excel shows Chinese correctly
        self.statusBar().showMessage(f"Exported {len(g.entries)} terms.", 4000)

    def glossary_history(self) -> None:
        if not self.project:
            return
        dlg = HistoryDialog(self, "Glossary history", self.project.glossary_history(), Project.read_history)
        if dlg.exec() and dlg.selected:
            self.project.restore_glossary(dlg.selected.path)
            self.g_model.set_glossary(self.project.glossary)
            self._refresh_glossary_views()
            self.log_line(f"Restored glossary from {dlg.selected.timestamp}.")

    # ================================================================== warnings
    def _refresh_warnings(self) -> None:
        ws = self.chapter.warnings if self.chapter else []
        self.w_table.setRowCount(len(ws))
        names = {"uncertain": "Uncertain", "missing": "Missing term", "conflict": "Conflict"}
        for r, w in enumerate(ws):
            vals = [names.get(w.kind, w.kind), w.zh, w.suggested_en, w.alternative_en, w.category,
                    "" if w.paragraph is None else str(w.paragraph + 1), w.reason]
            for c, v in enumerate(vals):
                item = QTableWidgetItem(v)
                if c == 0:
                    item.setForeground(QColor("#c87f0a") if w.kind == "uncertain" else QColor("#c0392b"))
                self.w_table.setItem(r, c, item)
        n = len(ws)
        self.tabs.setTabText(1, f"Warnings ({n})" if n else "Warnings")
        self._refresh_glossary_views()

    def _selected_warning_rows(self) -> list[int]:
        return sorted({i.row() for i in self.w_table.selectedIndexes()})

    def _jump_to_warning(self, row: int, _col: int = 0) -> None:
        if not self.chapter or row >= len(self.chapter.warnings):
            return
        w = self.chapter.warnings[row]
        if w.paragraph is not None:
            self.frame_a.show_linked_paragraph(w.paragraph)
            if self._aligned():
                self.frame_b.show_linked_paragraph(w.paragraph)

    def _drop_warnings_for(self, zh: str) -> None:
        if self.chapter and zh:
            self.chapter.warnings = [w for w in self.chapter.warnings if not (w.kind == "uncertain" and w.zh == zh)]
            self._refresh_warnings()
            self.save_chapter(quiet=True)

    def warning_to_glossary(self) -> None:
        rows = self._selected_warning_rows()
        if not rows or not self.chapter:
            return
        w = self.chapter.warnings[rows[0]]
        if w.kind == "missing":
            self.find_in_glossary(w.zh)
            return
        gender = next((g for g in ("female", "male") if g in w.reason.lower()), "")
        entry = GlossaryEntry(zh=w.zh, en=w.suggested_en, category=w.category, gender=gender, notes="",
                              first_seen=self.chapter.id)
        if self._edit_or_add(entry, "Add warning to glossary", [w.alternative_en] if w.alternative_en else []):
            self._drop_warnings_for(w.zh)

    def accept_warning_suggestion(self, use_alternative: bool = False) -> None:
        if not self.chapter:
            return
        added = 0
        swaps: list[tuple[str, str]] = []
        for r in self._selected_warning_rows():
            w = self.chapter.warnings[r]
            en = (w.alternative_en or w.suggested_en) if use_alternative else w.suggested_en
            if use_alternative and w.alternative_en and w.suggested_en and w.alternative_en != w.suggested_en:
                swaps.append((w.suggested_en, w.alternative_en))
            if w.kind != "uncertain" or not w.zh or not en or self.project.glossary.find_by_key(w.zh):
                continue
            gender = next((g for g in ("female", "male") if g in w.reason.lower()), "")
            self.g_model.add_entry(GlossaryEntry(zh=w.zh, en=en, category=w.category, gender=gender,
                                                 confirmed=True, first_seen=self.chapter.id))
            added += 1
        if swaps:
            # Swap the rendering in Frame B directly (Ctrl+Z undoes it); no AI call needed.
            text = self.frame_b.toPlainText()
            for old, new in swaps:
                pattern = (r"\b" if old[0].isalnum() else "") + re.escape(old) + (r"\b" if old[-1].isalnum() else "")
                text = re.sub(pattern, new.replace("\\", r"\\"), text)
            self.frame_b.set_text_undoable(text)
            self._sync_chapter_from_ui()
            self.log_line("Switched rendering: " + "; ".join(f"{o} → {n}" for o, n in swaps))
        zhs = {self.chapter.warnings[r].zh for r in self._selected_warning_rows()}
        self.chapter.warnings = [w for w in self.chapter.warnings if not (w.kind == "uncertain" and w.zh in zhs)]
        self._refresh_warnings()
        self.save_chapter(quiet=True)
        self.statusBar().showMessage(f"Added {added} term(s) to the glossary.", 4000)

    def dismiss_warnings(self) -> None:
        if not self.chapter:
            return
        rows = set(self._selected_warning_rows())
        self.chapter.warnings = [w for i, w in enumerate(self.chapter.warnings) if i not in rows]
        self._refresh_warnings()
        self.save_chapter(quiet=True)

    def check_consistency(self) -> None:
        if not (self.project and self.chapter):
            return
        self._sync_chapter_from_ui()
        g = self.project.glossary
        kept = [w for w in self.chapter.warnings if w.kind == "uncertain" and not g.find_by_key(w.zh)]
        missing = check_consistency(g, self.chapter.raw, self.chapter.translation)
        self.chapter.warnings = kept + missing
        self._refresh_warnings()
        self.save_chapter(quiet=True)
        self.statusBar().showMessage(f"Consistency check: {len(missing)} missing glossary term(s).", 5000)

    # ================================================================== AI plumbing
    def _translator(self, settings: Settings | None = None) -> Translator:
        s = settings or self.settings
        bridge = ClaudeBridge(s.claude_path, s.model, s.effort, s.timeout_seconds)
        naming = self.project.naming if self.project else None
        return Translator(bridge, s.chunk_chars, s.context_paragraphs, s.summary_effort, naming)

    def _run_ai(self, title: str, fn, on_done, on_partial=None, on_cancel=None) -> None:
        if self.worker:
            QMessageBox.information(self, "Busy", "An AI operation is already running.")
            return
        w = workers.Worker(fn)
        self.worker = w
        w.signals.progress.connect(lambda m: self.statusBar().showMessage(m))
        if on_partial:
            w.signals.partial.connect(on_partial)

        def finish():
            self.worker = None
            self.busy.hide()
            self.a_cancel.setEnabled(False)
            self._set_project_enabled(self.project is not None)
            for b in self.ai_buttons:
                b.setEnabled(True)

        def done(result):
            finish()
            try:
                on_done(result)
            except Exception as e:  # keep the app alive on unexpected UI-side errors
                import traceback
                QMessageBox.critical(self, f"{title} failed", traceback.format_exc())
                self.log_line(f"{title}: error {e}")

        def failed(msg):
            finish()
            self.statusBar().showMessage(f"{title} failed.", 8000)
            self.log_line(f"{title} failed: {msg}")
            QMessageBox.warning(self, f"{title} failed", msg)

        def cancelled():
            finish()
            self.statusBar().showMessage(f"{title} cancelled.", 6000)
            self.log_line(f"{title} cancelled.")
            if on_cancel:
                on_cancel()

        w.signals.finished.connect(done)
        w.signals.failed.connect(failed)
        w.signals.cancelled.connect(cancelled)
        self.busy.show()
        self.a_cancel.setEnabled(True)
        for a in self.ai_actions:
            a.setEnabled(False)
        for b in self.ai_buttons:
            b.setEnabled(False)
        self.statusBar().showMessage(f"{title}: starting Claude Code…")
        workers.start(w)

    def cancel_ai(self) -> None:
        if self.worker:
            self.worker.cancel()
            self.statusBar().showMessage("Cancelling…")

    def _log_stats(self, title: str, stats: TotalStats) -> None:
        self.log_line(
            f"{title}: {stats.calls} call(s), {stats.input_tokens:,} input / {stats.output_tokens:,} output tokens "
            f"(≈${stats.cost_usd:.3f} API-equivalent)."
        )
        parts = []
        if stats.five_hour_utilization is not None:
            parts.append(f"5h {stats.five_hour_utilization:.0%}")
        if stats.seven_day_utilization is not None:
            parts.append(f"7d {stats.seven_day_utilization:.0%}")
        if parts:
            self.usage_label.setText("Plan usage: " + " · ".join(parts))

    def log_line(self, text: str) -> None:
        from datetime import datetime

        self.log.appendPlainText(f"[{datetime.now():%H:%M:%S}] {text}")

    def _snapshot_inputs(self):
        self._sync_chapter_from_ui()
        self._save_notes()
        return (
            self.chapter.raw,
            self.chapter.translation,
            Glossary(copy.deepcopy(self.project.glossary.entries)),
            self.style_edit.toPlainText(),
            self.summary_edit.toPlainText(),
        )

    def _require_chapter(self) -> bool:
        if not (self.project and self.chapter):
            QMessageBox.information(self, "No chapter", "Open a project and select a chapter first.")
            return False
        return True

    # ================================================================== Sync
    def sync(self) -> None:
        if not self._require_chapter():
            return
        raw, translation, glossary, _style, _summary = self._snapshot_inputs()
        if not raw.strip() or not translation.strip():
            QMessageBox.information(self, "Sync", "Sync needs the Chinese chapter in Frame A and its English translation in Frame B.")
            return
        self.save_chapter(quiet=True)
        translator = self._translator()
        chapter_id = self.chapter.id

        def work(progress, cancel, partial):
            return translator.sync(raw, translation, glossary, progress, cancel)

        def done(outcome):
            self._log_stats("Sync", outcome.stats)
            changes = diff_proposals(self.project.glossary, outcome.terms)
            if not changes and not outcome.style_observations:
                QMessageBox.information(self, "Sync", f"Found {len(outcome.terms)} terms — the glossary already has all of them.")
                return
            dlg = SyncReviewDialog(self, changes, outcome.style_observations)
            if not dlg.exec():
                self.log_line("Sync results discarded.")
                return
            self.project.snapshot_glossary("before-sync")
            n = apply_changes(self.project.glossary, changes, chapter_id)
            self.g_model.refresh()
            self._on_glossary_changed()
            obs = dlg.selected_style_observations()
            if obs:
                text = self.style_edit.toPlainText().rstrip()
                self.style_edit.setPlainText(text + "\n" + "\n".join(f"- {o}" for o in obs) + "\n")
            self.log_line(f"Sync applied {n} glossary change(s){f' and {len(obs)} style note(s)' if obs else ''}.")
            self.statusBar().showMessage(f"Sync: applied {n} change(s).", 6000)
            self.check_consistency()

        self._run_ai("Sync", work, done)

    # ================================================================== Translate
    def translate(self) -> None:
        if not self._require_chapter():
            return
        raw, translation, glossary, style, summary = self._snapshot_inputs()
        if not raw.strip():
            QMessageBox.information(self, "Translate", "Paste a Chinese chapter into Frame A first.")
            return
        if translation.strip() and QMessageBox.question(
            self, "Translate", "Replace the current text in Frame B?\nThe current version is saved in the chapter history."
        ) != QMessageBox.Yes:
            return
        self.project.save_chapter(self.chapter)
        if translation.strip():
            self.project.add_history(self.chapter.id, translation, "before-translate")
        translator = self._translator()
        raw_paras = split_paragraphs(raw)
        self._partial = {}
        chapter = self.chapter

        def show(result: dict[int, str]) -> None:
            self._partial = result
            if self.chapter is chapter:
                done_upto = [result[i] for i in range(len(raw_paras)) if i in result]
                self.frame_b.set_text_undoable(join_paragraphs(done_upto))
                self.statusBar().showMessage(f"Translated {len(result)}/{len(raw_paras)} paragraphs…")

        def work(progress, cancel, partial):
            return translator.translate_chapter(raw, glossary, style, summary, progress, cancel, on_chunk=partial)

        def apply(outcome: TranslateOutcome) -> None:
            paras = [outcome.paragraphs.get(i, UNTRANSLATED_MARK + p) for i, p in enumerate(raw_paras)]
            if self.chapter is not chapter:  # user switched chapter meanwhile: save to the right one
                chapter.translation = join_paragraphs(paras)
            else:
                self.frame_b.set_text_undoable(join_paragraphs(paras))
                self._sync_chapter_from_ui()
            chapter.source = "ai"
            missing = check_consistency(self.project.glossary, chapter.raw, chapter.translation)
            chapter.warnings = outcome.warnings + missing
            self.project.save_chapter(chapter)
            if self.chapter is chapter:
                self._refresh_warnings()
            self._reload_chapter_list(select=self.chapter.id if self.chapter else None)

        def done(outcome: TranslateOutcome) -> None:
            apply(outcome)
            self._log_stats("Translate", outcome.stats)
            unc = sum(1 for w in chapter.warnings if w.kind == "uncertain")
            mis = sum(1 for w in chapter.warnings if w.kind == "missing")
            self.statusBar().showMessage(f"Translated {len(raw_paras)} paragraphs · {unc} uncertain term(s) · {mis} missing.", 10000)
            if chapter.warnings:
                self.tabs.setCurrentIndex(1)
            if self.settings.update_summary:
                QTimer.singleShot(0, lambda: self.update_summary(manual=False, chapter=chapter))

        def cancelled() -> None:
            if self._partial:
                apply(TranslateOutcome(dict(self._partial)))
                self.log_line(f"Kept {len(self._partial)} translated paragraphs; the rest are marked ⟦untranslated⟧ — "
                              "use Fix flagged to finish them.")

        self._run_ai("Translate", work, done, on_partial=show, on_cancel=cancelled)

    # ================================================================== Retranslate
    def retranslate_selection(self) -> None:
        if not self._require_chapter():
            return
        indices = self._selected_paragraphs()
        if not indices:
            QMessageBox.information(self, "Retranslate", "Put the cursor in a paragraph (or select several) in Frame A or B.")
            return
        self.retranslate(indices)

    def retranslate(self, indices: list[int], instruction: str | None = None, ask: bool = True) -> None:
        if not self._require_chapter():
            return
        if not self._aligned():
            QMessageBox.information(
                self, "Retranslate",
                "Frames A and B need the same number of paragraphs to retranslate individual paragraphs. "
                "Use Translate for the whole chapter instead.",
            )
            return
        if ask:
            label = f"paragraph {indices[0] + 1}" if len(indices) == 1 else f"{len(indices)} paragraphs"
            instruction, ok = QInputDialog.getMultiLineText(
                self, "Retranslate", f"Retranslate {label}.\nOptional instruction (e.g. “more formal”, “she is the speaker”):", ""
            )
            if not ok:
                return
        raw, translation, glossary, style, summary = self._snapshot_inputs()
        self.project.save_chapter(self.chapter)
        translator = self._translator()
        chapter = self.chapter
        provisional = [copy.copy(w) for w in chapter.warnings if w.kind == "uncertain"]

        def work(progress, cancel, partial):
            return translator.retranslate(raw, translation, indices, glossary, style, summary, instruction or "",
                                          progress, cancel, provisional)

        def done(outcome: TranslateOutcome) -> None:
            self._log_stats("Retranslate", outcome.stats)
            if self.chapter is not chapter:
                return
            new_text = replace_paragraphs(self.frame_b.toPlainText(), outcome.paragraphs)
            self.frame_b.set_text_undoable(new_text)
            self._sync_chapter_from_ui()
            self.project.add_history(chapter.id, translation, "before-retranslate")
            changed = set(outcome.paragraphs)
            kept = [w for w in chapter.warnings if w.kind == "uncertain" and not self.project.glossary.find_by_key(w.zh)]
            known = {w.zh for w in kept}
            new = [w for w in outcome.warnings if w.zh not in known]
            chapter.warnings = kept + new + check_consistency(self.project.glossary, chapter.raw, chapter.translation)
            chapter.source = "ai"
            self.project.save_chapter(chapter)
            self._refresh_warnings()
            self.frame_b.show_linked_paragraph(min(changed) if changed else None)
            self.statusBar().showMessage(f"Retranslated {len(changed)} paragraph(s) (Ctrl+Z in Frame B to undo).", 8000)

        self._run_ai("Retranslate", work, done)

    def fix_flagged(self) -> None:
        if not self._require_chapter():
            return
        self.check_consistency()
        indices = {w.paragraph for w in self.chapter.warnings if w.kind == "missing" and w.paragraph is not None}
        for i, p in enumerate(split_paragraphs(self.frame_b.toPlainText())):
            if p.startswith(UNTRANSLATED_MARK):
                indices.add(i)
        if not indices:
            if not self._aligned() and any(w.kind == "missing" for w in self.chapter.warnings):
                QMessageBox.information(self, "Fix flagged", "Paragraphs are not aligned, so flagged terms can't be traced to "
                                        "paragraphs. Translate the chapter again instead.")
            else:
                QMessageBox.information(self, "Fix flagged", "Nothing to fix — no missing glossary terms or untranslated paragraphs.")
            return
        self.retranslate(sorted(indices), "Make sure every glossary term is translated exactly as the glossary says.", ask=False)

    # ================================================================== Summary
    def update_summary(self, manual: bool = True, chapter: Chapter | None = None) -> None:
        if not self._require_chapter():
            return
        chapter = chapter or self.chapter
        if chapter is self.chapter:
            self._sync_chapter_from_ui()
        if not chapter.translation.strip():
            if manual:
                QMessageBox.information(self, "Summary", "This chapter has no translation yet.")
            return
        old = self.summary_edit.toPlainText()
        translator = self._translator()
        label, text = chapter.label, chapter.translation

        def work(progress, cancel, partial):
            return translator.update_summary(old, label, text, progress, cancel)

        def done(result) -> None:
            summary, stats = result
            self._log_stats("Summary", stats)
            if self.summary_edit.toPlainText() != old:
                self.log_line("Story summary was edited during the update; the new summary was not applied.")
                return
            self.summary_edit.setPlainText(summary)
            self._save_notes()
            self.statusBar().showMessage("Story summary updated.", 5000)

        self._run_ai("Summary", work, done)

    # ================================================================== Settings
    def open_settings(self) -> None:
        voices = {lang: self.speaker.voice_names(lang) for lang in ("zh", "en")}
        dlg = SettingsDialog(self, self.settings, self.test_connection, voices)
        self._settings_dialog = dlg
        if dlg.exec():
            self.settings = dlg.collect()
            self.settings.save()
            self._apply_font()
            self._apply_speech_settings()
        self._settings_dialog = None

    _settings_dialog: SettingsDialog | None = None

    def test_connection(self, settings: Settings) -> None:
        translator = self._translator(settings)

        def report(msg: str) -> None:
            if self._settings_dialog is not None:
                self._settings_dialog.status.setText(msg)

        def work(progress, cancel, partial):
            return translator.ping(progress, cancel)

        def done(stats: TotalStats) -> None:
            self._log_stats("Connection test", stats)
            msg = f"✓ Claude Code is working ({stats.input_tokens:,} input tokens per minimal call)."
            report(msg)
            if self._settings_dialog is None:
                QMessageBox.information(self, "Connection test", msg)

        report("Testing…")
        self._run_ai("Connection test", work, done)

    # ================================================================== window
    # ================================================================== Pop-up dictionary
    def _dict_path(self) -> Path:
        return config_dir() / CEDICT_FILENAME

    def _load_dictionary(self) -> None:
        path = self._dict_path()
        if not path.exists():
            self.dict_panel.set_missing(True)
            return
        w = workers.Worker(lambda progress, cancel, partial: Dictionary.load(path))
        self._dict_worker = w

        def loaded(d: Dictionary) -> None:
            self._dict_worker = None
            self.dictionary = d
            self.dict_panel.set_missing(False)
            self.log_line(f"Dictionary: loaded {len(d):,} CC-CEDICT words.")

        def failed(msg: str) -> None:
            self._dict_worker = None
            self.log_line(f"Dictionary: could not load {path}: {msg}")
            self.dict_panel.set_missing(True)

        w.signals.finished.connect(loaded)
        w.signals.failed.connect(failed)
        workers.start(w)

    def download_dictionary(self) -> None:
        if self._dict_worker:
            return
        path = self._dict_path()
        w = workers.Worker(lambda progress, cancel, partial: download_cedict(path, progress))
        self._dict_worker = w
        self.dict_panel.b_download.setEnabled(False)
        w.signals.progress.connect(lambda m: self.statusBar().showMessage(m))

        def done(n: int) -> None:
            self._dict_worker = None
            self.dict_panel.b_download.setEnabled(True)
            self.statusBar().showMessage(f"Dictionary: downloaded {n:,} entries.", 5000)
            self._load_dictionary()

        def failed(msg: str) -> None:
            self._dict_worker = None
            self.dict_panel.b_download.setEnabled(True)
            self.statusBar().showMessage("Dictionary download failed.", 8000)
            QMessageBox.warning(self, "Dictionary download failed",
                                f"{msg}\n\nYou can also download cedict_1_0_ts_utf-8_mdbg.txt.gz from mdbg.net yourself, "
                                f"unzip it and save it as:\n{path}")

        w.signals.finished.connect(done)
        w.signals.failed.connect(failed)
        workers.start(w)

    def _toggle_hover_speak(self) -> None:
        self.settings.hover_speak = self.a_hover_speak.isChecked()
        self.settings.save()
        self._apply_speech_settings()
        if self.settings.hover_speak:
            secs = f"{self.settings.hover_speak_seconds:g}"
            self.statusBar().showMessage(f"Rest the mouse on a word or selected phrase for {secs} s to hear it.", 5000)

    def _toggle_auto_speak(self) -> None:
        self.settings.dict_auto_speak = self.a_auto_speak.isChecked()
        self.settings.save()

    def _on_lookup_shown(self, word: str) -> None:
        """Read a word aloud once when its pop-up dictionary appears, unless a paragraph is being read aloud."""
        if not self.settings.dict_auto_speak or not word.strip():
            return
        if self.speaker.is_speaking() and self._speech_frame is not None:
            return
        self._speech_frame = None
        self.speaker.speak(word)  # replaces a previous word still being spoken

    def speak_hovered(self, text: str) -> None:
        if self.speaker.is_speaking():
            return
        self._speech_frame = None
        self.speaker.speak(text)

    def _speak_unit_at(self, text: str, pos: int) -> tuple[int, int] | None:
        """(start, end) of what a long hover at text[pos] reads in Frame A: a glossary term, else the dictionary word,
        else the Latin word or single character."""
        g = self._glossary_at(text, pos)
        if g:
            return g[0], g[1]
        if self.dictionary:
            res = self.dictionary.look_up_at(text, pos)
            if res:
                return res.primary.start, res.primary.end
        for m in re.finditer(r"[A-Za-z0-9'’-]+", text):
            if m.start() <= pos < m.end():
                return m.span()
        return (pos, pos + 1) if text[pos].isalnum() else None

    def _toggle_hover(self) -> None:
        self.settings.dict_hover = self.a_hover.isChecked()
        self.settings.save()

    def _save_wordlist(self) -> None:
        try:
            self.wordlist.save()
        except OSError as e:
            self.log_line(f"Word list: could not save: {e}")

    def _source_label(self) -> str:
        if not self.project:
            return ""
        return self.project.name + (f" · {self.chapter.label}" if self.chapter else "")

    def _glossary_at(self, text: str, pos: int):
        """(start, end, entry) of the glossary term covering text[pos], or None."""
        if self.project:
            for s, e, entry in self.project.glossary.matches(text):
                if s <= pos < e:
                    return s, e, entry
        return None

    def _word_at(self, text: str, pos: int) -> str | None:
        """The word a lookup at text[pos] is about: a glossary term covering pos wins, else the longest dictionary word."""
        g = self._glossary_at(text, pos)
        if g:
            return text[g[0]:g[1]]
        if self.dictionary:
            res = self.dictionary.look_up_at(text, pos)
            if res:
                return res.word
        return None

    def _hover_lookup(self, _block, text: str, pos: int, gmatch):
        if not self.settings.dict_hover or self.dictionary is None:
            return None
        res = self.dictionary.look_up_at(text, pos)
        if res is None and gmatch is None:
            return None
        gentry = gmatch[2] if gmatch else None
        if gmatch:
            word, start, end = text[gmatch[0]:gmatch[1]], gmatch[0], gmatch[1]
        else:
            word, start, end = res.word, res.primary.start, res.primary.end
        sentence = res.sentence if res else sentence_at(text, pos)
        entries = self.dictionary.lookup(word)
        saved = self.wordlist.get(word)
        pinyin = entries[0].pinyin if entries else self.dictionary.guess_pinyin(word)
        if (entries or gentry) and word not in self._hover_seen:  # count once per chapter visit
            self._hover_seen.add(word)
            gloss = entries[0].short_gloss(60) if entries else (gentry.en if gentry else "")
            saved = self.wordlist.record_lookup(word, pinyin, gloss, sentence, self._source_label())
            self._wordlist_timer.start()
            self.dict_panel.refresh_list()
        self._show_in_panel(word, res, gentry, sentence)
        return tooltip_html(word, res, gentry, saved, entries, pinyin), start, end

    def _show_in_panel(self, word: str, result: LookupResult | None, gentry, sentence: str, status: str = "") -> None:
        self._dict_current = {"word": word, "result": result, "glossary": gentry, "sentence": sentence}
        saved = self.wordlist.get(word)
        if not status and self._explaining == word:
            status = "Asking Claude…"
        self.dict_panel.show_html(panel_html(word, self.dictionary, result, gentry, saved, sentence, status),
                                  bool(saved and saved.saved))
        self.dict_panel.b_explain.setText("💡 Explain again" if saved and saved.explanation else "💡 Explain")

    def _show_list_word(self, word: str) -> None:
        saved = self.wordlist.get(word)
        gentry = None
        if self.project:
            gentry = next((e for e in self.project.glossary.entries if e.zh == word), None)
        self._show_in_panel(word, None, gentry, saved.sentence if saved else "")

    def _target(self, source: str) -> dict | None:
        """The word to explain or star.

        source="auto" (shortcut): the selection in Frame A, else the word last shown in the panel (usually the hovered one),
        else the word at the text cursor. "cursor" (context menu): the selection, else the word at the cursor.
        "panel" (panel buttons): the word shown in the panel.
        """
        if source == "panel":
            return self._dict_current
        sel, block, pos = self.frame_a.selected_or_cursor()
        text = block.text()
        if sel:
            if len(sel) > 20 or "\n" in sel:
                return None
            g = self._glossary_at(text, pos)
            res = self.dictionary.look_up_at(text, pos) if self.dictionary else None
            return {"word": sel, "result": res if res and res.word == sel else None,
                    "glossary": g[2] if g and text[g[0]:g[1]] == sel else None, "sentence": sentence_at(text, pos)}
        if source == "auto" and self._dict_current:
            return self._dict_current
        word = self._word_at(text, pos)
        if not word:
            return None
        g = self._glossary_at(text, pos)
        res = self.dictionary.look_up_at(text, pos) if self.dictionary else None
        return {"word": word, "result": res, "glossary": g[2] if g else None, "sentence": sentence_at(text, pos)}

    def star_word(self, source: str = "auto") -> None:
        t = self._target(source)
        if not t:
            self.statusBar().showMessage("Star: hover a word or select one (up to 20 characters) in Frame A first.", 4000)
            return
        word = t["word"]
        entries = self.dictionary.lookup(word) if self.dictionary else []
        saved = self.wordlist.get(word)
        now = not (saved and saved.saved)
        gloss = entries[0].short_gloss(60) if entries else (t["glossary"].en if t["glossary"] else "")
        pinyin = entries[0].pinyin if entries else (self.dictionary.guess_pinyin(word) if self.dictionary else "")
        self.wordlist.set_saved(word, now, pinyin, gloss, t["sentence"], self._source_label())
        self._save_wordlist()
        self.dict_panel.refresh_list()
        self._show_in_panel(word, t["result"], t["glossary"], t["sentence"])
        self.statusBar().showMessage(f"{'Starred' if now else 'Unstarred'} {word}.", 3000)

    def speak_current_word(self) -> None:
        t = self._dict_current
        if not t:
            return
        self.stop_reading()
        self._speech_frame = None
        self.speaker.speak(t["word"])

    def explain_word(self, source: str = "auto") -> None:
        t = self._target(source)
        if not t:
            self.statusBar().showMessage("Explain: hover a word or select one (up to 20 characters) in Frame A first.", 4000)
            return
        word, sentence, gentry, result = t["word"], t["sentence"], t["glossary"], t["result"]
        saved = self.wordlist.get(word)
        force = source == "panel"  # the panel button reads "Explain again" once an answer is cached
        if saved and saved.explanation and not force:
            self.dict_dock.show()
            self._show_in_panel(word, result, gentry, sentence)
            return
        if self._explain_worker:
            self.statusBar().showMessage(f"Still explaining {self._explaining}…", 4000)
            return
        entries = self.dictionary.lookup(word) if self.dictionary else []
        dict_hint = "\n".join(f"{e.pinyin}: {'; '.join(e.definitions)}" for e in entries[:4])
        gloss_hint = ""
        if gentry:
            gloss_hint = f"{gentry.zh} → {gentry.en} ({gentry.category})" + (f"; {gentry.notes}" if gentry.notes else "")
        s = self.settings
        bridge = ClaudeBridge(s.claude_path, s.model, s.effort, s.timeout_seconds)
        novel = self.project.name if self.project else ""
        effort = s.explain_effort
        source_label = self._source_label()
        w = workers.Worker(lambda progress, cancel, partial: explain_word(
            bridge, word, sentence, progress, cancel, novel, dict_hint, gloss_hint, effort))
        self._explain_worker, self._explaining = w, word
        w.signals.progress.connect(lambda m: self.statusBar().showMessage(m))

        def finish() -> None:
            self._explain_worker, self._explaining = None, ""

        def done(res) -> None:
            finish()
            data, stats = res
            self.wordlist.set_explanation(word, data, sentence, source_label)
            self._save_wordlist()
            self.dict_panel.refresh_list()
            self._log_stats(f"Explain {word}", stats)
            self.statusBar().showMessage(f"Explained {word}.", 4000)
            self._show_in_panel(word, result, gentry, sentence)

        def failed(msg: str) -> None:
            finish()
            self.log_line(f"Explain {word} failed: {msg}")
            self._show_in_panel(word, result, gentry, sentence, status=f"Explain failed: {msg[:300]}")

        w.signals.finished.connect(done)
        w.signals.failed.connect(failed)
        w.signals.cancelled.connect(finish)
        self.dict_dock.show()
        self._show_in_panel(word, result, gentry, sentence, status="Asking Claude…")
        workers.start(w)

    def closeEvent(self, e):  # noqa: N802
        if self.worker:
            if QMessageBox.question(self, "Quit", "An AI operation is running. Cancel it and quit?") != QMessageBox.Yes:
                e.ignore()
                return
            self.worker.cancel()
        if self._explain_worker:
            self._explain_worker.cancel()
        self.save_all(quiet=True)
        self._save_wordlist()
        self.settings.save()
        super().closeEvent(e)
