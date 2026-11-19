"""Read-aloud support: speaks selected text from Frame A / Frame B with the system's text-to-speech voices."""

from __future__ import annotations

from PySide6.QtCore import QLocale, QObject, Signal
from PySide6.QtTextToSpeech import QTextToSpeech, QVoice

from ..text_utils import detect_language

# Windows OneCore voices (winrt) include the downloadable Chinese voices; SAPI usually only has the desktop ones.
ENGINE_PREFERENCE = ["winrt", "sapi", "darwin", "speechd", "flite", "android"]

LANGUAGES = {"zh": QLocale.Chinese, "en": QLocale.English}

INSTALL_HINT = (
    "No {lang} text-to-speech voice is installed.\n\n"
    "To add one on Windows: Settings → Time & language → Language & region → Add a language → "
    "{example}, and tick “Text-to-speech” (or open the language's options and download Speech). "
    "Restart LN Translator afterwards."
)


class Speaker(QObject):
    """Wraps QTextToSpeech and picks a Chinese or English voice depending on the text."""

    speakingChanged = Signal(bool)
    wordSpoken = Signal(int, int)  # start, length relative to the spoken text
    error = Signal(str)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._tts: QTextToSpeech | None = None
        self._failed = False
        self.voice_prefs: dict[str, str] = {"zh": "", "en": ""}
        self.rate = 0.0

    # ----- engine -------------------------------------------------------------
    def _engine(self) -> QTextToSpeech | None:
        if self._tts is None and not self._failed:
            available = [e for e in QTextToSpeech.availableEngines() if e != "mock"]
            ordered = [e for e in ENGINE_PREFERENCE if e in available] + [e for e in available if e not in ENGINE_PREFERENCE]
            if not ordered:
                self._failed = True
                return None
            self._tts = QTextToSpeech(ordered[0], self)
            self._tts.stateChanged.connect(self._on_state)
            self._tts.sayingWord.connect(lambda _word, _id, start, length: self.wordSpoken.emit(start, length))
            self._tts.errorOccurred.connect(lambda _reason, msg: self.error.emit(msg or "Text-to-speech error."))
        return self._tts

    def available(self) -> bool:
        return self._engine() is not None

    def voices(self, lang: str) -> list[QVoice]:
        tts = self._engine()
        if tts is None:
            return []
        voices: dict[str, QVoice] = {}
        for loc in tts.availableLocales():
            if loc.language() == LANGUAGES[lang]:
                for v in tts.allVoices(loc):
                    voices.setdefault(v.name(), v)
        return list(voices.values())

    def voice_names(self, lang: str) -> list[str]:
        return [v.name() for v in self.voices(lang)]

    def _pick_voice(self, lang: str) -> QVoice | None:
        voices = self.voices(lang)
        if not voices:
            return None
        preferred = self.voice_prefs.get(lang, "")
        for v in voices:
            if v.name() == preferred:
                return v
        if lang == "zh":  # prefer Mandarin (mainland) over Cantonese/Taiwanese voices
            for v in voices:
                if v.locale().territory() == QLocale.China:
                    return v
        return voices[0]

    # ----- speaking -----------------------------------------------------------
    def is_speaking(self) -> bool:
        return self._tts is not None and self._tts.state() in (QTextToSpeech.Speaking, QTextToSpeech.Synthesizing)

    def speak(self, text: str) -> bool:
        """Speak text with a voice matching its language. Returns False (and emits error) if impossible."""
        if not text.strip():
            return False
        tts = self._engine()
        if tts is None:
            self.error.emit("No text-to-speech engine is available on this system.")
            return False
        lang = detect_language(text)
        voice = self._pick_voice(lang)
        if voice is None:
            names = {"zh": ("Chinese", "Chinese (Simplified, China)"), "en": ("English", "English (United States)")}
            self.error.emit(INSTALL_HINT.format(lang=names[lang][0], example=names[lang][1]))
            return False
        tts.stop()
        tts.setVoice(voice)
        tts.setRate(max(-1.0, min(1.0, self.rate)))
        tts.say(text)
        return True

    def stop(self) -> None:
        if self._tts is not None:
            self._tts.stop()

    def _on_state(self, state) -> None:
        self.speakingChanged.emit(state in (QTextToSpeech.Speaking, QTextToSpeech.Synthesizing))
