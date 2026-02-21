from app.language_support import (
    DEFAULT_SOURCE_LANGUAGE,
    DEFAULT_TARGET_LANGUAGE,
    build_translation_system_prompt,
    resolve_supported_language,
    resolve_translation_languages,
)


def test_resolve_supported_language_with_alias():
    lang = resolve_supported_language("es")
    assert lang is not None
    assert lang.code == "es-US"


def test_resolve_translation_languages_normalizes_aliases():
    source, target = resolve_translation_languages("en", "fr-FR")
    assert source == "en-US"
    assert target == "fr-FR"


def test_resolve_translation_languages_rejects_equal_languages():
    try:
        resolve_translation_languages("en-US", "en-US")
    except ValueError as exc:
        assert "must be different" in str(exc)
    else:
        raise AssertionError("Expected ValueError")


def test_build_translation_system_prompt_mentions_languages():
    prompt = build_translation_system_prompt(DEFAULT_SOURCE_LANGUAGE, DEFAULT_TARGET_LANGUAGE)
    assert "English (US)" in prompt
    assert "Spanish (US)" in prompt
