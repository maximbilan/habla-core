"""Supported Nova Sonic languages and translation helpers."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SupportedLanguage:
    code: str
    name: str
    locale: str
    default_voice_id: str

    @property
    def label(self) -> str:
        return f"{self.name} ({self.locale})"


SUPPORTED_NOVA_LANGUAGES: dict[str, SupportedLanguage] = {
    "en-US": SupportedLanguage("en-US", "English", "US", "matthew"),
    "en-GB": SupportedLanguage("en-GB", "English", "UK", "amy"),
    "en-AU": SupportedLanguage("en-AU", "English", "Australia", "olivia"),
    "en-IN": SupportedLanguage("en-IN", "English", "India", "kiara"),
    "es-US": SupportedLanguage("es-US", "Spanish", "US", "lupe"),
    "fr-FR": SupportedLanguage("fr-FR", "French", "France", "remi"),
    "de-DE": SupportedLanguage("de-DE", "German", "Germany", "florian"),
    "it-IT": SupportedLanguage("it-IT", "Italian", "Italy", "lorenzo"),
    "pt-BR": SupportedLanguage("pt-BR", "Portuguese", "Brazil", "leo"),
    "hi-IN": SupportedLanguage("hi-IN", "Hindi", "India", "arjun"),
}

DEFAULT_SOURCE_LANGUAGE = "en-US"
DEFAULT_TARGET_LANGUAGE = "es-US"

LANGUAGE_ALIASES: dict[str, str] = {
    "en": "en-US",
    "es": "es-US",
    "fr": "fr-FR",
    "de": "de-DE",
    "it": "it-IT",
    "pt": "pt-BR",
    "hi": "hi-IN",
}


def normalize_language_code(code: str) -> str:
    return code.strip().replace("_", "-")


def resolve_supported_language(code: str) -> SupportedLanguage | None:
    normalized = normalize_language_code(code)

    direct = SUPPORTED_NOVA_LANGUAGES.get(normalized)
    if direct:
        return direct

    lower_normalized = normalized.lower()
    if lower_normalized in LANGUAGE_ALIASES:
        return SUPPORTED_NOVA_LANGUAGES[LANGUAGE_ALIASES[lower_normalized]]

    for candidate_code, candidate in SUPPORTED_NOVA_LANGUAGES.items():
        if candidate_code.lower() == lower_normalized:
            return candidate

    return None


def resolve_translation_languages(
    source_language: str,
    target_language: str,
) -> tuple[str, str]:
    source = resolve_supported_language(source_language)
    target = resolve_supported_language(target_language)

    if not source:
        raise ValueError(f"Unsupported source_language '{source_language}'")
    if not target:
        raise ValueError(f"Unsupported target_language '{target_language}'")
    if source.code == target.code:
        raise ValueError("source_language and target_language must be different")

    return source.code, target.code


def build_translation_system_prompt(source_language: str, target_language: str) -> str:
    source = SUPPORTED_NOVA_LANGUAGES[source_language]
    target = SUPPORTED_NOVA_LANGUAGES[target_language]
    return (
        "You are a real-time voice translator. Listen to the user speaking in "
        f"{source.label} and respond by saying the exact same message translated "
        f"into natural, conversational {target.label}. Do not add any commentary, "
        "greetings, or explanations. Only output the translation of what was said. "
        "Maintain the same tone and intent. If the user pauses, wait for them to continue."
    )


def default_voice_id_for_language(language_code: str) -> str:
    return SUPPORTED_NOVA_LANGUAGES[language_code].default_voice_id


def supported_languages_payload() -> list[dict[str, str]]:
    return [
        {
            "code": language.code,
            "name": language.name,
            "locale": language.locale,
            "default_voice_id": language.default_voice_id,
        }
        for language in SUPPORTED_NOVA_LANGUAGES.values()
    ]
