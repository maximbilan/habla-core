from app.language_support import (
    DEFAULT_SOURCE_LANGUAGE,
    DEFAULT_TARGET_LANGUAGE,
    build_translation_system_prompt,
    normalize_voice_gender,
    resolve_supported_language,
    resolve_translation_languages,
    voice_id_for_language,
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


def test_build_translation_system_prompt_is_concise_for_fast_call_mode():
    prompt = build_translation_system_prompt(DEFAULT_SOURCE_LANGUAGE, DEFAULT_TARGET_LANGUAGE)
    lowered = prompt.lower()
    assert "real-time speech translator" in lowered
    assert "respond only with translated speech" in lowered
    assert "deliver short translated chunks immediately" in lowered


def test_normalize_voice_gender_accepts_none_and_known_values():
    assert normalize_voice_gender(None) is None
    assert normalize_voice_gender("female") == "female"
    assert normalize_voice_gender(" MALE ") == "male"


def test_normalize_voice_gender_rejects_unknown_value():
    try:
        normalize_voice_gender("robot")
    except ValueError as exc:
        assert "voice_gender must be either 'female' or 'male'" in str(exc)
    else:
        raise AssertionError("Expected ValueError")


def test_voice_id_for_language_uses_gender_overrides_for_english():
    assert voice_id_for_language("en-US", "male") == "matthew"
    assert voice_id_for_language("en-US", "female") == "amy"


def test_voice_id_for_language_uses_male_fallback_for_spanish_us():
    assert voice_id_for_language("es-US", "male") == "matthew"
    assert voice_id_for_language("es-US", "female") == "lupe"
