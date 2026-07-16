"""Offline tests for per-document language detection (pytest).

The resolution *policy* (override / detect / fallback) is tested with a
scripted detector so it is deterministic and needs no model. A couple of
tests exercise the real py3langid identifier on document-length text —
that is the case it is reliable in, and the case ingestion actually hits.
"""

from __future__ import annotations

import yizkr.pipeline.language as lang
from yizkr.pipeline.language import (
    AUTO,
    LanguageResolution,
    detect_language,
    resolve_language,
)

DE = (
    "Berlin, den 8. Dezember 1937. Sehr geehrter Herr Direktor, hiermit "
    "bestaetige ich den Empfang Ihres Schreibens. Die Steuerangelegenheit "
    "des Herrn Berthold ist nunmehr geklaert und der Betrag beglichen."
)
EN = (
    "I was born in Berlin in 1929, the youngest of three children. My "
    "father ran a small flour mill on the outskirts of the city, and my "
    "earliest memories are of the smell of grain and the turning wheels."
)


class TestResolutionPolicy:
    def test_explicit_code_forces_and_skips_detection(self, monkeypatch):
        def boom(_text):  # detection must not be consulted for a forced code
            raise AssertionError("detection should not run for an explicit code")

        monkeypatch.setattr(lang, "detect_language", boom)
        result = resolve_language("egal welcher Text", "de")
        assert result == LanguageResolution("de", "override")

    def test_auto_uses_confident_detection(self, monkeypatch):
        monkeypatch.setattr(lang, "detect_language", lambda _t: ("de", 0.98))
        result = resolve_language("...", AUTO, fallback="en")
        assert result.code == "de"
        assert result.source == "detected"
        assert result.confidence == 0.98

    def test_auto_falls_back_below_floor(self, monkeypatch):
        # a real but low-confidence guess: keep the guess as context, use
        # the fallback code
        monkeypatch.setattr(lang, "detect_language", lambda _t: ("cs", 0.3))
        result = resolve_language("kurz", AUTO, fallback="en", floor=0.5)
        assert result.code == "en"
        assert result.source == "fallback"
        assert result.confidence == 0.3

    def test_auto_falls_back_when_undetectable(self, monkeypatch):
        monkeypatch.setattr(lang, "detect_language", lambda _t: (None, 0.0))
        result = resolve_language("x", AUTO, fallback="de")
        assert result == LanguageResolution("de", "fallback", None)

    def test_floor_boundary_is_inclusive(self, monkeypatch):
        monkeypatch.setattr(lang, "detect_language", lambda _t: ("fr", 0.5))
        assert resolve_language("...", AUTO, floor=0.5).source == "detected"


class TestDescribe:
    def test_each_source_has_a_distinct_phrasing(self):
        assert "forced" in LanguageResolution("de", "override").describe()
        assert "detected" in LanguageResolution("de", "detected", 0.97).describe()
        assert "fallback" in LanguageResolution("en", "fallback", 0.3).describe()
        assert "too short" in LanguageResolution("en", "fallback", None).describe()


class TestRealDetector:
    def test_document_length_text_is_identified(self):
        code, confidence = detect_language(DE)
        assert code == "de"
        assert confidence > 0.9
        code, confidence = detect_language(EN)
        assert code == "en"
        assert confidence > 0.9

    def test_too_short_is_not_guessed(self):
        assert detect_language("Berthold") == (None, 0.0)
        assert detect_language("") == (None, 0.0)
        assert detect_language("   ") == (None, 0.0)

    def test_detected_code_matches_monolingual_pattern(self):
        # a detected code must be writable as a source excerpt's language
        from yizkr.claims import MonolingualText

        code, _ = detect_language(DE)
        MonolingualText(text="x", language=code)  # must not raise
