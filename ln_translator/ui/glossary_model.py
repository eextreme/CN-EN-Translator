"""Qt table model for Frame C (the glossary) plus filtering and a category delegate."""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt
from PySide6.QtWidgets import QComboBox, QStyledItemDelegate

from ..glossary import Glossary
from ..models import CATEGORIES, GENDERS, GlossaryEntry, split_aliases

COLUMNS = [
    ("confirmed", "✓"),
    ("zh", "Chinese"),
    ("en", "English"),
    ("category", "Category"),
    ("aliases", "Aliases"),
    ("gender", "Gender"),
    ("notes", "Notes"),
    ("first_seen", "First seen"),
]
COL = {key: i for i, (key, _) in enumerate(COLUMNS)}


class GlossaryModel(QAbstractTableModel):
    def __init__(self, glossary: Glossary, on_change: Callable[[], None]):
        super().__init__()
        self.glossary = glossary
        self.on_change = on_change

    def set_glossary(self, glossary: Glossary) -> None:
        self.beginResetModel()
        self.glossary = glossary
        self.endResetModel()

    def refresh(self) -> None:
        self.beginResetModel()
        self.endResetModel()

    # ----- Qt API -----------------------------------------------------------
    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.glossary.entries)

    def columnCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):  # noqa: N802
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return COLUMNS[section][1]
        if orientation == Qt.Horizontal and role == Qt.ToolTipRole and section == 0:
            return "Confirmed by you — Sync will not overwrite it"
        return None

    def entry(self, row: int) -> GlossaryEntry:
        return self.glossary.entries[row]

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        e = self.entry(index.row())
        key = COLUMNS[index.column()][0]
        if key == "confirmed":
            if role == Qt.CheckStateRole:
                return Qt.Checked if e.confirmed else Qt.Unchecked
            return None
        if role in (Qt.DisplayRole, Qt.EditRole):
            if key == "aliases":
                return ", ".join(e.aliases)
            return getattr(e, key)
        if role == Qt.ToolTipRole and key == "notes" and e.notes:
            return e.notes
        return None

    def flags(self, index):
        base = Qt.ItemIsEnabled | Qt.ItemIsSelectable
        if COLUMNS[index.column()][0] == "confirmed":
            return base | Qt.ItemIsUserCheckable
        return base | Qt.ItemIsEditable

    def setData(self, index, value, role=Qt.EditRole):  # noqa: N802
        if not index.isValid():
            return False
        e = self.entry(index.row())
        key = COLUMNS[index.column()][0]
        if key == "confirmed" and role == Qt.CheckStateRole:
            e.confirmed = Qt.CheckState(value) == Qt.Checked
        elif role == Qt.EditRole:
            value = str(value).strip()
            if key == "aliases":
                e.aliases = split_aliases(value)
            elif key == "zh" and not value:
                return False
            else:
                setattr(e, key, value)
            if key == "en" or key == "zh":
                e.confirmed = True  # a manual edit is a confirmation
        else:
            return False
        self.glossary.invalidate()
        self.dataChanged.emit(self.index(index.row(), 0), self.index(index.row(), len(COLUMNS) - 1))
        self.on_change()
        return True

    # ----- row operations ---------------------------------------------------
    def add_entry(self, entry: GlossaryEntry) -> int:
        row = len(self.glossary.entries)
        self.beginInsertRows(QModelIndex(), row, row)
        self.glossary.add(entry)
        self.endInsertRows()
        self.on_change()
        return row

    def remove_rows(self, rows: list[int]) -> None:
        ids = [self.entry(r).id for r in rows]
        self.beginResetModel()
        for i in ids:
            self.glossary.remove(i)
        self.endResetModel()
        self.on_change()

    def row_of(self, entry_id: str) -> int:
        for i, e in enumerate(self.glossary.entries):
            if e.id == entry_id:
                return i
        return -1


class GlossaryFilter(QSortFilterProxyModel):
    def __init__(self):
        super().__init__()
        self.text = ""
        self.category = ""
        self.only_unconfirmed = False
        self.in_chapter: set[str] | None = None  # entry ids that occur in the current chapter
        self.setSortCaseSensitivity(Qt.CaseInsensitive)

    def set_filters(self, text: str, category: str, only_unconfirmed: bool) -> None:
        self.text, self.category, self.only_unconfirmed = text.strip().lower(), category, only_unconfirmed
        self.invalidateFilter()

    def filterAcceptsRow(self, row, parent):  # noqa: N802
        model: GlossaryModel = self.sourceModel()
        e = model.entry(row)
        if self.category and e.category != self.category:
            return False
        if self.only_unconfirmed and e.confirmed:
            return False
        if self.in_chapter is not None and e.id not in self.in_chapter:
            return False
        if self.text:
            hay = " ".join([e.zh, e.en, " ".join(e.aliases), e.notes]).lower()
            return self.text in hay
        return True


class ChoiceDelegate(QStyledItemDelegate):
    def __init__(self, choices: list[str], parent=None):
        super().__init__(parent)
        self.choices = choices

    def createEditor(self, parent, option, index):  # noqa: N802
        box = QComboBox(parent)
        box.addItems(self.choices)
        return box

    def setEditorData(self, editor, index):  # noqa: N802
        value = index.data(Qt.EditRole) or ""
        i = editor.findText(value)
        editor.setCurrentIndex(max(i, 0))

    def setModelData(self, editor, model, index):  # noqa: N802
        model.setData(index, editor.currentText(), Qt.EditRole)


def category_delegate(parent=None) -> ChoiceDelegate:
    return ChoiceDelegate(CATEGORIES, parent)


def gender_delegate(parent=None) -> ChoiceDelegate:
    return ChoiceDelegate(GENDERS, parent)
