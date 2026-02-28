"""Goal-oriented extraction and completion tracking for Agent Mode calls."""

from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata
from typing import Iterable

from app.extraction_patterns import (
    ADDRESS_SUFFIX_PATTERN,
    LOCATION_PREFIX_PATTERN,
    MONTH_PATTERN,
    NAME_INTRO_PATTERN,
    NEXT_STEP_PREFIX_PATTERN,
    WEEKDAY_PATTERN,
)

GOAL_CONFIDENCE_THRESHOLD = 0.67

_FIELD_ALIASES: dict[str, str] = {
    "appointment_date": "date",
    "appointment_day": "date",
    "day": "date",
    "appointment_time": "time",
    "hour": "time",
    "location": "location",
    "address": "location",
    "place": "location",
    "price": "price",
    "amount": "price",
    "cost": "price",
    "fee": "price",
    "nextstep": "next_step",
    "next_step": "next_step",
    "follow_up": "next_step",
    "action": "next_step",
    "phone": "phone_number",
    "phone_number": "phone_number",
    "name": "name",
}

_FIELD_LABEL_EN: dict[str, str] = {
    "date": "date",
    "time": "time",
    "location": "location/address",
    "price": "price",
    "next_step": "next step",
    "phone_number": "phone number",
    "name": "name",
}

_FIELD_LABEL_ES: dict[str, str] = {
    "date": "fecha",
    "time": "hora",
    "location": "ubicacion/direccion",
    "price": "precio",
    "next_step": "siguiente paso",
    "phone_number": "numero de telefono",
    "name": "nombre",
}


@dataclass(slots=True)
class GoalFieldState:
    name: str
    value: str
    confidence: float
    occurrences: int
    source_role: str

    def payload(self) -> dict[str, object]:
        return {
            "name": self.name,
            "value": self.value,
            "confidence": round(self.confidence, 3),
            "occurrences": self.occurrences,
            "source_role": self.source_role,
            "verified": self.confidence >= GOAL_CONFIDENCE_THRESHOLD,
        }

    def result_payload(self) -> dict[str, object]:
        return {
            "value": self.value,
            "confidence": round(self.confidence, 3),
            "occurrences": self.occurrences,
            "source_role": self.source_role,
            "verified": self.confidence >= GOAL_CONFIDENCE_THRESHOLD,
        }


def normalize_goal_field_name(name: str) -> str:
    normalized = _normalize_text(name).replace(" ", "_")
    normalized = re.sub(r"[^a-z0-9_]", "", normalized)
    return _FIELD_ALIASES.get(normalized, normalized)


def normalize_goal_required_fields(fields: Iterable[str]) -> list[str]:
    normalized_fields: list[str] = []
    seen: set[str] = set()

    for raw in fields:
        normalized = normalize_goal_field_name(raw)
        if not normalized:
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        normalized_fields.append(normalized)

    return normalized_fields


class GoalTracker:
    """Tracks required goal fields and extracts values from transcript text."""

    def __init__(self, objective: str, required_fields: Iterable[str]) -> None:
        self.objective = (objective or "").strip()
        self.required_fields = normalize_goal_required_fields(required_fields)
        self._field_state: dict[str, GoalFieldState] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.required_fields)

    def observe_text(self, *, role: str, text: str) -> bool:
        if not self.enabled:
            return False

        changed = False
        for field_name in self.required_fields:
            extracted = self._extract_field(field_name, text)
            if not extracted:
                continue
            value, confidence = extracted
            changed = self._upsert_field(
                field_name=field_name,
                value=value,
                confidence=confidence,
                role=role,
            ) or changed

        return changed

    def observe_translation_pair(self, *, source_text: str, translated_text: str) -> bool:
        changed_source = self.observe_text(role="translation_source", text=source_text)
        changed_target = self.observe_text(role="translation_target", text=translated_text)
        return changed_source or changed_target

    def missing_fields(self, *, threshold: float = GOAL_CONFIDENCE_THRESHOLD) -> list[str]:
        missing: list[str] = []
        for field_name in self.required_fields:
            state = self._field_state.get(field_name)
            if not state or state.confidence < threshold:
                missing.append(field_name)
        return missing

    def completion_rate(self, *, threshold: float = GOAL_CONFIDENCE_THRESHOLD) -> float:
        if not self.required_fields:
            return 1.0
        complete = len(self.required_fields) - len(self.missing_fields(threshold=threshold))
        return complete / len(self.required_fields)

    def needs_follow_up(self, *, threshold: float = GOAL_CONFIDENCE_THRESHOLD) -> bool:
        return bool(self.missing_fields(threshold=threshold))

    def build_follow_up_instruction(self, *, threshold: float = GOAL_CONFIDENCE_THRESHOLD) -> str:
        missing = self.missing_fields(threshold=threshold)
        if not missing:
            return ""

        human_fields = ", ".join(_FIELD_LABEL_EN.get(name, name.replace("_", " ")) for name in missing)
        return (
            "Before ending the call, the objective is still incomplete. "
            f"Ask concise follow-up questions to capture: {human_fields}. "
            "Ask one focused question at a time, confirm each value, then close politely."
        )

    def progress_payload(self) -> dict[str, object]:
        missing = self.missing_fields()
        return {
            "type": "goal_progress",
            "objective": self.objective,
            "required_fields": self.required_fields,
            "fields": [self._field_state[name].payload() for name in self.required_fields if name in self._field_state],
            "missing_fields": missing,
            "completion_rate": round(self.completion_rate(), 3),
            "success": not missing,
        }

    def result_payload(self) -> dict[str, object]:
        missing = self.missing_fields()
        success = not missing
        fields_map = {
            name: state.result_payload()
            for name, state in self._field_state.items()
            if name in self.required_fields
        }
        summary_en = self._summary_en(fields_map=fields_map, missing_fields=missing)
        summary_es = self._summary_es(fields_map=fields_map, missing_fields=missing)

        return {
            "type": "goal_result_summary",
            "objective": self.objective,
            "required_fields": self.required_fields,
            "fields": [self._field_state[name].payload() for name in self.required_fields if name in self._field_state],
            "missing_fields": missing,
            "completion_rate": round(self.completion_rate(), 3),
            "success": success,
            "summary_en": summary_en,
            "summary_es": summary_es,
            "result": {
                "objective": self.objective,
                "required_fields": self.required_fields,
                "fields": fields_map,
                "missing_fields": missing,
                "success": success,
            },
        }

    def _summary_en(self, *, fields_map: dict[str, dict[str, object]], missing_fields: list[str]) -> str:
        captured_items = [
            f"{_FIELD_LABEL_EN.get(name, name)}: {fields_map[name]['value']}"
            for name in self.required_fields
            if name in fields_map
        ]
        captured_text = ", ".join(captured_items) if captured_items else "none"
        missing_text = (
            ", ".join(_FIELD_LABEL_EN.get(name, name) for name in missing_fields)
            if missing_fields
            else "none"
        )
        objective = self.objective or "Task completion"
        return f"Objective: {objective}. Captured: {captured_text}. Missing: {missing_text}."

    def _summary_es(self, *, fields_map: dict[str, dict[str, object]], missing_fields: list[str]) -> str:
        captured_items = [
            f"{_FIELD_LABEL_ES.get(name, name)}: {fields_map[name]['value']}"
            for name in self.required_fields
            if name in fields_map
        ]
        captured_text = ", ".join(captured_items) if captured_items else "ninguno"
        missing_text = (
            ", ".join(_FIELD_LABEL_ES.get(name, name) for name in missing_fields)
            if missing_fields
            else "ninguno"
        )
        objective = self.objective or "Completar la tarea"
        return f"Objetivo: {objective}. Capturado: {captured_text}. Faltante: {missing_text}."

    def _upsert_field(self, *, field_name: str, value: str, confidence: float, role: str) -> bool:
        new_value = value.strip()
        if not new_value:
            return False

        new_confidence = max(0.0, min(confidence, 0.99))
        existing = self._field_state.get(field_name)

        if not existing:
            self._field_state[field_name] = GoalFieldState(
                name=field_name,
                value=new_value,
                confidence=new_confidence,
                occurrences=1,
                source_role=role,
            )
            return True

        if _normalize_text(existing.value) == _normalize_text(new_value):
            existing.occurrences += 1
            boosted = min(0.99, max(existing.confidence, new_confidence) + 0.06)
            if abs(boosted - existing.confidence) > 0.001:
                existing.confidence = boosted
                existing.source_role = role
                return True
            existing.source_role = role
            return False

        replacement_score = new_confidence + 0.03
        existing_score = existing.confidence + (0.02 * existing.occurrences)
        if replacement_score >= existing_score:
            existing.value = new_value
            existing.confidence = new_confidence
            existing.occurrences = 1
            existing.source_role = role
            return True

        return False

    def _extract_field(self, field_name: str, text: str) -> tuple[str, float] | None:
        stripped = (text or "").strip()
        if not stripped:
            return None

        lowered = stripped.lower()

        if field_name == "date":
            return _extract_date(stripped)
        if field_name == "time":
            return _extract_time(stripped)
        if field_name == "location":
            return _extract_location(stripped, lowered)
        if field_name == "price":
            return _extract_price(stripped)
        if field_name == "next_step":
            return _extract_next_step(stripped, lowered)
        if field_name == "phone_number":
            return _extract_phone(stripped)
        if field_name == "name":
            return _extract_name(stripped, lowered)

        # Generic fallback: capture phrase following "<field> is ..."
        field_label = field_name.replace("_", " ")
        generic_match = re.search(
            rf"(?:{re.escape(field_label)}|{re.escape(field_name)})\s*(?:is|=|:)\s*([^,.!?]+)",
            stripped,
            flags=re.IGNORECASE,
        )
        if generic_match:
            return generic_match.group(1).strip(), 0.62

        return None


def _extract_date(text: str) -> tuple[str, float] | None:
    numeric = re.search(r"\b\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?\b", text)
    if numeric:
        return numeric.group(0), 0.81

    month_name = re.search(
        rf"\b{MONTH_PATTERN}\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,\s*\d{{2,4}})?\b",
        text,
        flags=re.IGNORECASE,
    )
    if month_name:
        return month_name.group(0), 0.82

    weekday = re.search(rf"\b{WEEKDAY_PATTERN}\b", text, flags=re.IGNORECASE)
    if weekday:
        return weekday.group(0), 0.68

    return None


def _extract_time(text: str) -> tuple[str, float] | None:
    ampm = re.search(r"\b\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?|am|pm)\b", text, flags=re.IGNORECASE)
    if ampm:
        return ampm.group(0), 0.84

    twenty_four = re.search(r"\b(?:[01]?\d|2[0-3]):[0-5]\d\b", text)
    if twenty_four:
        return twenty_four.group(0), 0.8

    oclock = re.search(r"\b\d{1,2}\s*o'?clock\b", text, flags=re.IGNORECASE)
    if oclock:
        return oclock.group(0), 0.72

    return None


def _extract_location(text: str, lowered: str) -> tuple[str, float] | None:
    street = re.search(
        rf"\b\d{{1,5}}\s+[A-Za-z0-9.\- ]+\s{ADDRESS_SUFFIX_PATTERN}\b",
        text,
        flags=re.IGNORECASE,
    )
    if street:
        return street.group(0).strip(), 0.83

    keyword = re.search(rf"{LOCATION_PREFIX_PATTERN}\s*[:\-]?\s*([^,.!?]+)", text, flags=re.IGNORECASE)
    if keyword:
        return keyword.group(1).strip(), 0.75

    if "at " in lowered and len(text) < 120:
        at_phrase = re.search(r"\bat\s+([A-Za-z0-9][^,.!?]{4,80})", text, flags=re.IGNORECASE)
        if at_phrase:
            return at_phrase.group(1).strip(), 0.64

    return None


def _extract_price(text: str) -> tuple[str, float] | None:
    currency_prefixed = re.search(r"(?:\$|€|£)\s?\d+(?:[.,]\d{1,2})?", text)
    if currency_prefixed:
        return currency_prefixed.group(0), 0.9

    currency_suffixed = re.search(
        r"\b\d+(?:[.,]\d{1,2})?\s?(?:usd|eur|dollars?|euros?|pounds?)\b",
        text,
        flags=re.IGNORECASE,
    )
    if currency_suffixed:
        return currency_suffixed.group(0), 0.86

    return None


def _extract_next_step(text: str, lowered: str) -> tuple[str, float] | None:
    keyword = re.search(rf"{NEXT_STEP_PREFIX_PATTERN}\s*[:\-]?\s*([^.!?]+)", text, flags=re.IGNORECASE)
    if keyword:
        value = keyword.group(1).strip()
        if len(value) >= 6:
            return value, 0.69

    if len(text) <= 110 and ("next" in lowered or "step" in lowered):
        return text.strip(), 0.62

    return None


def _extract_phone(text: str) -> tuple[str, float] | None:
    match = re.search(r"(?:\+?\d[\d\-\s().]{6,}\d)", text)
    if not match:
        return None

    value = " ".join(match.group(0).split())
    return value, 0.88


def _extract_name(text: str, lowered: str) -> tuple[str, float] | None:
    intro = re.search(rf"{NAME_INTRO_PATTERN}\s+([A-ZÁÉÍÓÚÑ][^\d,.!?]{1,60})", text, flags=re.IGNORECASE)
    if intro:
        value = intro.group(1).strip()
        if len(value.split()) <= 5:
            return value, 0.74

    if lowered.startswith("name:"):
        value = text.split(":", 1)[1].strip()
        if value:
            return value, 0.72

    return None


def _normalize_text(value: str) -> str:
    normalized = " ".join((value or "").strip().lower().split())
    normalized = "".join(
        ch for ch in unicodedata.normalize("NFKD", normalized) if not unicodedata.combining(ch)
    )
    return normalized
