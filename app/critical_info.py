"""Critical fact detection and verification helpers for live calls."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

FACT_TYPE_NAME = "name"
FACT_TYPE_PHONE = "phone_number"
FACT_TYPE_DATE = "date"
FACT_TYPE_ADDRESS = "address"
FACT_TYPE_MONEY = "money_amount"

FACT_TYPES = (
    FACT_TYPE_NAME,
    FACT_TYPE_PHONE,
    FACT_TYPE_DATE,
    FACT_TYPE_ADDRESS,
    FACT_TYPE_MONEY,
)

_FACT_LABELS_EN = {
    FACT_TYPE_NAME: "name",
    FACT_TYPE_PHONE: "phone number",
    FACT_TYPE_DATE: "date",
    FACT_TYPE_ADDRESS: "address",
    FACT_TYPE_MONEY: "money amount",
}

_FACT_LABELS_ES = {
    FACT_TYPE_NAME: "nombre",
    FACT_TYPE_PHONE: "numero de telefono",
    FACT_TYPE_DATE: "fecha",
    FACT_TYPE_ADDRESS: "direccion",
    FACT_TYPE_MONEY: "monto",
}

_MONTH_PATTERN = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|"
    r"dec(?:ember)?|enero|febrero|marzo|abril|mayo|junio|julio|agosto|"
    r"septiembre|setiembre|octubre|noviembre|diciembre)"
)

_PHONE_RE = re.compile(r"(?<!\w)(?:\+?\d[\d\s\-()]{7,}\d)(?!\w)")
_DATE_SLASH_RE = re.compile(r"\b\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?\b", re.IGNORECASE)
_DATE_MONTH_LEADING_RE = re.compile(
    rf"\b{_MONTH_PATTERN}\s+\d{{1,2}}(?:,?\s+\d{{2,4}})?\b",
    re.IGNORECASE,
)
_DATE_DAY_LEADING_RE = re.compile(
    rf"\b\d{{1,2}}\s+de\s+{_MONTH_PATTERN}(?:\s+de\s+\d{{2,4}})?\b",
    re.IGNORECASE,
)
_MONEY_RE = re.compile(
    r"(?:[$€£]\s?\d[\d,]*(?:\.\d{1,2})?|"
    r"\d[\d,]*(?:\.\d{1,2})?\s?(?:usd|eur|gbp|cad|mxn|dollars?|euros?|pesos?))",
    re.IGNORECASE,
)
_ADDRESS_RE = re.compile(
    r"\b\d{1,5}\s+[A-Za-z0-9\s\-.]{2,60}\s"
    r"(?:street|st|avenue|ave|road|rd|drive|dr|lane|ln|boulevard|blvd|"
    r"court|ct|place|pl|way|calle|avenida|av|camino|paseo)\b",
    re.IGNORECASE,
)
_NAME_RE = re.compile(
    r"\b(?:my name is|this is|i am|i'm|me llamo|mi nombre es|soy)\s+"
    r"([A-Za-z\u00C0-\u017F]+(?:\s+[A-Za-z\u00C0-\u017F]+){0,3})\b",
    re.IGNORECASE,
)


@dataclass(slots=True)
class DetectedFact:
    fact_type: str
    value: str
    normalized: str
    confidence: float


@dataclass(slots=True)
class ConfirmationPrompt:
    fact_type: str
    reason: str
    source_value: str | None
    candidate_value: str
    confidence: float
    prompt_en: str
    prompt_es: str

    def to_payload(self) -> dict[str, object]:
        return {
            "type": "critical_confirmation",
            "fact_type": self.fact_type,
            "reason": self.reason,
            "source_value": self.source_value,
            "candidate_value": self.candidate_value,
            "confidence": round(self.confidence, 3),
            "prompt_en": self.prompt_en,
            "prompt_es": self.prompt_es,
        }


@dataclass(slots=True)
class _FactState:
    fact_type: str
    normalized: str
    value: str
    confidence: float
    first_seen: float
    last_seen: float
    occurrences: int = 1
    verified: bool = False
    last_role: str = ""

    def to_payload(self) -> dict[str, object]:
        return {
            "type": self.fact_type,
            "value": self.value,
            "confidence": round(self.confidence, 3),
            "verified": self.verified,
            "occurrences": self.occurrences,
            "last_role": self.last_role,
        }


class CriticalInfoTracker:
    """Tracks high-risk facts and builds confirmation prompts + call summary."""

    def __init__(
        self,
        *,
        low_confidence_threshold: float = 0.82,
        verified_confidence_threshold: float = 0.9,
        confirmation_cooldown_seconds: float = 9.0,
    ) -> None:
        self._low_confidence_threshold = low_confidence_threshold
        self._verified_confidence_threshold = verified_confidence_threshold
        self._confirmation_cooldown_seconds = confirmation_cooldown_seconds

        self._facts_by_key: dict[str, _FactState] = {}
        self._last_confirmation_by_signature: dict[str, float] = {}
        self._last_fact_by_type: dict[str, _FactState] = {}

    def observe_text(self, *, role: str, text: str) -> list[ConfirmationPrompt]:
        facts = self.extract(text)
        if not facts:
            return []

        prompts: list[ConfirmationPrompt] = []
        for fact in facts:
            current_state = self._upsert_fact(role=role, fact=fact)
            previous_state = self._last_fact_by_type.get(fact.fact_type)

            if previous_state and previous_state.normalized != current_state.normalized:
                if self._looks_like_variation(
                    fact_type=fact.fact_type,
                    left=previous_state.normalized,
                    right=current_state.normalized,
                ):
                    prompt = self._build_intracall_mismatch_prompt(
                        previous_value=previous_state.value,
                        current_fact=fact,
                    )
                    if self._should_emit_confirmation(prompt):
                        prompts.append(prompt)

            self._last_fact_by_type[fact.fact_type] = current_state

            if fact.confidence < self._low_confidence_threshold:
                prompt = self._build_low_confidence_prompt(fact)
                if self._should_emit_confirmation(prompt):
                    prompts.append(prompt)
        return prompts

    def observe_translation_pair(
        self,
        *,
        source_text: str,
        translated_text: str,
    ) -> list[ConfirmationPrompt]:
        source_facts = self.extract(source_text)
        translated_facts = self.extract(translated_text)
        if not source_facts or not translated_facts:
            return []

        source_by_type = self._group_by_type(source_facts)
        translated_by_type = self._group_by_type(translated_facts)

        prompts: list[ConfirmationPrompt] = []
        for fact_type in FACT_TYPES:
            source_items = source_by_type.get(fact_type)
            translated_items = translated_by_type.get(fact_type)
            if not source_items or not translated_items:
                continue

            source_norms = {item.normalized for item in source_items}
            translated_norms = {item.normalized for item in translated_items}
            if source_norms.intersection(translated_norms):
                continue

            source_item = max(source_items, key=lambda item: item.confidence)
            translated_item = max(translated_items, key=lambda item: item.confidence)
            prompt = self._build_translation_mismatch_prompt(
                source_item=source_item,
                translated_item=translated_item,
            )
            if self._should_emit_confirmation(prompt):
                prompts.append(prompt)
        return prompts

    def summary_payload(self, *, verified_only: bool = False) -> dict[str, object]:
        return {
            "type": "verified_facts_summary",
            "facts": self.summary_facts(verified_only=verified_only),
        }

    def summary_facts(self, *, verified_only: bool = False) -> list[dict[str, object]]:
        items = list(self._facts_by_key.values())
        if verified_only:
            items = [item for item in items if item.verified]

        items.sort(
            key=lambda item: (
                not item.verified,
                -item.confidence,
                -item.occurrences,
                -item.last_seen,
            )
        )
        return [item.to_payload() for item in items]

    def extract(self, text: str) -> list[DetectedFact]:
        raw_text = (text or "").strip()
        if not raw_text:
            return []

        candidates: list[tuple[int, DetectedFact]] = []
        seen_keys: set[str] = set()

        for match in _PHONE_RE.finditer(raw_text):
            value = match.group(0).strip()
            self._append_candidate(
                candidates,
                seen_keys,
                start=match.start(),
                fact_type=FACT_TYPE_PHONE,
                value=value,
                confidence=self._phone_confidence(value),
            )

        for regex in (_DATE_SLASH_RE, _DATE_MONTH_LEADING_RE, _DATE_DAY_LEADING_RE):
            for match in regex.finditer(raw_text):
                value = match.group(0).strip()
                self._append_candidate(
                    candidates,
                    seen_keys,
                    start=match.start(),
                    fact_type=FACT_TYPE_DATE,
                    value=value,
                    confidence=self._date_confidence(value),
                )

        for match in _MONEY_RE.finditer(raw_text):
            value = match.group(0).strip()
            self._append_candidate(
                candidates,
                seen_keys,
                start=match.start(),
                fact_type=FACT_TYPE_MONEY,
                value=value,
                confidence=self._money_confidence(value),
            )

        for match in _ADDRESS_RE.finditer(raw_text):
            value = match.group(0).strip()
            self._append_candidate(
                candidates,
                seen_keys,
                start=match.start(),
                fact_type=FACT_TYPE_ADDRESS,
                value=value,
                confidence=0.84,
            )

        for match in _NAME_RE.finditer(raw_text):
            value = match.group(1).strip()
            self._append_candidate(
                candidates,
                seen_keys,
                start=match.start(1),
                fact_type=FACT_TYPE_NAME,
                value=value,
                confidence=0.88,
            )

        candidates.sort(key=lambda item: item[0])
        return [item[1] for item in candidates]

    def _append_candidate(
        self,
        candidates: list[tuple[int, DetectedFact]],
        seen_keys: set[str],
        *,
        start: int,
        fact_type: str,
        value: str,
        confidence: float,
    ) -> None:
        normalized = self._normalize_value(fact_type, value)
        if not normalized:
            return

        dedupe_key = f"{fact_type}:{normalized}"
        if dedupe_key in seen_keys:
            return
        seen_keys.add(dedupe_key)

        candidates.append(
            (
                start,
                DetectedFact(
                    fact_type=fact_type,
                    value=" ".join(value.split()),
                    normalized=normalized,
                    confidence=max(0.0, min(1.0, confidence)),
                ),
            )
        )

    def _upsert_fact(self, *, role: str, fact: DetectedFact) -> _FactState:
        now = time.time()
        key = f"{fact.fact_type}:{fact.normalized}"
        state = self._facts_by_key.get(key)
        if not state:
            state = _FactState(
                fact_type=fact.fact_type,
                normalized=fact.normalized,
                value=fact.value,
                confidence=fact.confidence,
                first_seen=now,
                last_seen=now,
                last_role=role,
            )
            self._facts_by_key[key] = state
        else:
            state.last_seen = now
            state.occurrences += 1
            state.confidence = max(state.confidence, fact.confidence)
            state.last_role = role
            if len(fact.value) > len(state.value):
                state.value = fact.value

        if state.confidence >= self._verified_confidence_threshold:
            state.verified = True
        elif state.occurrences >= 2 and state.confidence >= self._low_confidence_threshold:
            state.verified = True

        return state

    def _build_low_confidence_prompt(self, fact: DetectedFact) -> ConfirmationPrompt:
        label_en = _FACT_LABELS_EN.get(fact.fact_type, fact.fact_type)
        label_es = _FACT_LABELS_ES.get(fact.fact_type, fact.fact_type)
        candidate = fact.value
        return ConfirmationPrompt(
            fact_type=fact.fact_type,
            reason="low_confidence",
            source_value=None,
            candidate_value=candidate,
            confidence=fact.confidence,
            prompt_en=f"Please confirm the {label_en}: '{candidate}'.",
            prompt_es=f"Por favor confirma el/la {label_es}: '{candidate}'.",
        )

    def _build_translation_mismatch_prompt(
        self,
        *,
        source_item: DetectedFact,
        translated_item: DetectedFact,
    ) -> ConfirmationPrompt:
        label_en = _FACT_LABELS_EN.get(source_item.fact_type, source_item.fact_type)
        label_es = _FACT_LABELS_ES.get(source_item.fact_type, source_item.fact_type)
        confidence = min(source_item.confidence, translated_item.confidence)
        return ConfirmationPrompt(
            fact_type=source_item.fact_type,
            reason="value_changed_in_translation",
            source_value=source_item.value,
            candidate_value=translated_item.value,
            confidence=confidence,
            prompt_en=(
                f"I heard the {label_en} as '{source_item.value}', "
                f"but translation says '{translated_item.value}'. Which is correct?"
            ),
            prompt_es=(
                f"Escuche el/la {label_es} como '{source_item.value}', "
                f"pero la traduccion dice '{translated_item.value}'. Cual es correcto?"
            ),
        )

    def _build_intracall_mismatch_prompt(
        self,
        *,
        previous_value: str,
        current_fact: DetectedFact,
    ) -> ConfirmationPrompt:
        label_en = _FACT_LABELS_EN.get(current_fact.fact_type, current_fact.fact_type)
        label_es = _FACT_LABELS_ES.get(current_fact.fact_type, current_fact.fact_type)
        return ConfirmationPrompt(
            fact_type=current_fact.fact_type,
            reason="value_changed_in_call",
            source_value=previous_value,
            candidate_value=current_fact.value,
            confidence=current_fact.confidence,
            prompt_en=(
                f"I previously heard the {label_en} as '{previous_value}', "
                f"and now I heard '{current_fact.value}'. Please confirm."
            ),
            prompt_es=(
                f"Antes escuche el/la {label_es} como '{previous_value}', "
                f"y ahora escuche '{current_fact.value}'. Por favor confirma."
            ),
        )

    def _looks_like_variation(self, *, fact_type: str, left: str, right: str) -> bool:
        if not left or not right:
            return False

        if fact_type == FACT_TYPE_PHONE:
            left_digits = re.sub(r"\D", "", left)
            right_digits = re.sub(r"\D", "", right)
            if len(left_digits) != len(right_digits):
                return False
            if len(left_digits) < 7:
                return False
            same_positions = sum(1 for x, y in zip(left_digits, right_digits) if x == y)
            return same_positions >= max(5, len(left_digits) - 2)

        if fact_type == FACT_TYPE_MONEY:
            left_num = re.sub(r"[^\d.-]", "", left)
            right_num = re.sub(r"[^\d.-]", "", right)
            try:
                left_value = float(left_num)
                right_value = float(right_num)
            except ValueError:
                return False
            if left_value == 0:
                return False
            return abs(left_value - right_value) / abs(left_value) <= 0.35

        if fact_type == FACT_TYPE_DATE:
            left_tokens = set(re.findall(r"\w+", left.lower()))
            right_tokens = set(re.findall(r"\w+", right.lower()))
            return bool(left_tokens.intersection(right_tokens))

        if fact_type in {FACT_TYPE_NAME, FACT_TYPE_ADDRESS}:
            left_head = left.split(" ")[0].lower()
            right_head = right.split(" ")[0].lower()
            return left_head == right_head

        return False

    def _should_emit_confirmation(self, prompt: ConfirmationPrompt) -> bool:
        source_key = (prompt.source_value or "").strip().lower()
        candidate_key = prompt.candidate_value.strip().lower()
        signature = f"{prompt.reason}|{prompt.fact_type}|{source_key}|{candidate_key}"

        now = time.monotonic()
        previous = self._last_confirmation_by_signature.get(signature)
        if previous is not None and now - previous < self._confirmation_cooldown_seconds:
            return False

        self._last_confirmation_by_signature[signature] = now
        return True

    def _group_by_type(self, facts: list[DetectedFact]) -> dict[str, list[DetectedFact]]:
        grouped: dict[str, list[DetectedFact]] = {}
        for fact in facts:
            grouped.setdefault(fact.fact_type, []).append(fact)
        return grouped

    def _normalize_value(self, fact_type: str, value: str) -> str:
        collapsed = " ".join((value or "").strip().split())
        if not collapsed:
            return ""

        if fact_type == FACT_TYPE_PHONE:
            digits = re.sub(r"\D", "", collapsed)
            if len(digits) < 7:
                return ""
            return f"+{digits}" if collapsed.startswith("+") else digits

        if fact_type == FACT_TYPE_MONEY:
            numeric = re.sub(r"[^\d.,-]", "", collapsed)
            normalized_numeric = numeric.replace(",", "")
            return normalized_numeric or collapsed.lower()

        return collapsed.lower()

    def _phone_confidence(self, value: str) -> float:
        digits = re.sub(r"\D", "", value)
        if len(digits) >= 10:
            return 0.93
        if len(digits) >= 8:
            return 0.84
        return 0.72

    def _date_confidence(self, value: str) -> float:
        lowered = value.lower()
        if re.search(r"\d{4}", lowered):
            return 0.91
        if re.search(_MONTH_PATTERN, lowered, re.IGNORECASE):
            return 0.88
        return 0.8

    def _money_confidence(self, value: str) -> float:
        lowered = value.lower()
        if any(symbol in lowered for symbol in ("$", "€", "£")):
            return 0.92
        return 0.84
