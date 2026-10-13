"""Выбор изданий по профилю языков, покрытию и лицензионной политике."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from app.catalog.models import TranslationMeta
from app.catalog.policy import LicenseDecision, decide_license
from app.catalog.profiles import preferred_ids, profile_languages


@dataclass(frozen=True, slots=True)
class SelectionItem:
    """Кандидат: описание издания, решение по лицензии и оценка пригодности."""
    metadata: TranslationMeta
    decision: LicenseDecision
    score: tuple[int, int, int, int, str]


@dataclass(slots=True)
class SelectionResult:
    """Итог выбора: выбранные, отклонённые, недостающие языки и кандидаты по языкам."""
    selected: list[SelectionItem]
    rejected: list[SelectionItem]
    missing_languages: list[str]
    candidates_by_language: dict[str, list[SelectionItem]]


def score_translation(metadata: TranslationMeta) -> tuple[int, int, int, int, str]:
    """Оценка издания для сортировки: покрытие (полная Библия лучше), предпочтительный идентификатор из профиля, число стихов, сертификация и идентификатор для стабильного порядка."""
    preferred = preferred_ids(metadata.language_code)
    identifier = metadata.translation_id.lower()
    preference_score = 0
    for position, marker in enumerate(preferred):
        if marker == identifier:
            preference_score = 10_000 - position
            break
        if marker and marker in identifier:
            preference_score = 5_000 - position
            break

    coverage_score = {
        "full": 4,
        "nt": 3,
        "ot": 2,
        "partial": 1,
        "unknown": 0,
    }[metadata.coverage]
    certified = 1 if str(metadata.extra.get("Certified", "")).lower() == "true" else 0
    return (
        coverage_score,
        preference_score,
        metadata.total_verses,
        certified,
        metadata.translation_id.lower(),
    )


def select_translations(
    catalog: list[TranslationMeta],
    *,
    profile: str,
    max_editions_per_language: int = 1,
    allow_restricted: bool = False,
    allow_unknown: bool = False,
) -> SelectionResult:
    """Выбрать издания из каталога: оставить язык профиля, проверить лицензию, отсортировать по оценке и взять лучшие `max_editions_per_language` (для `all-open` — все допустимые). Возвращает также отклонённые и языки, для которых ничего не нашлось."""
    target_languages = profile_languages(profile)
    target_set = set(target_languages) if target_languages is not None else None
    accepted_by_language: dict[str, list[SelectionItem]] = defaultdict(list)
    rejected: list[SelectionItem] = []

    for metadata in catalog:
        if target_set is not None and metadata.language_code not in target_set:
            continue
        decision = decide_license(
            metadata,
            allow_restricted=allow_restricted,
            allow_unknown=allow_unknown,
        )
        item = SelectionItem(metadata, decision, score_translation(metadata))
        if decision.allowed:
            accepted_by_language[metadata.language_code].append(item)
        else:
            rejected.append(item)

    selected: list[SelectionItem] = []
    for _language_code, items in accepted_by_language.items():
        items.sort(key=lambda item: item.score, reverse=True)
        if profile == "all-open":
            selected.extend(items)
        else:
            selected.extend(items[:max_editions_per_language])

    selected.sort(key=lambda item: (item.metadata.language_code, item.metadata.translation_id))
    rejected.sort(key=lambda item: (item.metadata.language_code, item.metadata.translation_id))

    missing_languages: list[str] = []
    if target_languages is not None:
        missing_languages = sorted(set(target_languages) - set(accepted_by_language))

    return SelectionResult(
        selected,
        rejected,
        missing_languages,
        {key: list(value) for key, value in accepted_by_language.items()},
    )
