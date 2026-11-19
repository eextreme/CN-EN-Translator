"""Run blocking AI calls off the UI thread."""

from __future__ import annotations

import threading
import traceback
from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

from ..claude_bridge import Cancelled, ClaudeError


class WorkerSignals(QObject):
    progress = Signal(str)
    partial = Signal(object)
    finished = Signal(object)
    failed = Signal(str)
    cancelled = Signal()


class Worker(QRunnable):
    """Calls fn(progress=..., cancel=..., partial=...) on a pool thread."""

    def __init__(self, fn: Callable[..., Any]):
        super().__init__()
        self.fn = fn
        self.cancel_event = threading.Event()
        self.signals = WorkerSignals()
        self.setAutoDelete(False)

    def run(self) -> None:  # noqa: D401 - Qt API
        try:
            result = self.fn(
                progress=self.signals.progress.emit,
                cancel=self.cancel_event,
                partial=self.signals.partial.emit,
            )
        except Cancelled:
            self.signals.cancelled.emit()
        except (ClaudeError, ValueError, RuntimeError) as e:
            if self.cancel_event.is_set():
                self.signals.cancelled.emit()
            else:
                self.signals.failed.emit(str(e))
        except Exception:  # pragma: no cover - unexpected bug, show the trace
            self.signals.failed.emit(traceback.format_exc())
        else:
            if self.cancel_event.is_set():
                self.signals.cancelled.emit()
            else:
                self.signals.finished.emit(result)

    def cancel(self) -> None:
        self.cancel_event.set()


def start(worker: Worker) -> None:
    QThreadPool.globalInstance().start(worker)
