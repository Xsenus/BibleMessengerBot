"""Catalog selection reporting used by CLI and admin UI."""

from __future__ import annotations

from collections import Counter
from typing import Any

from app.catalog.selector import SelectionResult


def selection_summary(result: SelectionResult) -> dict[str, Any]:
    selected_languages = Counter(item.metadata.language_code for item in result.selected)
    rejected_reasons = Counter(item.decision.reason for item in result.rejected)
    return {
        "selected_editions": len(result.selected),
        "selected_languages": len(selected_languages),
        "editions_per_language": dict(sorted(selected_languages.items())),
        "missing_languages": result.missing_languages,
        "fallback_candidates": sum(len(items) for items in result.candidates_by_language.values()),
        "rejected_editions": len(result.rejected),
        "rejected_reasons": dict(rejected_reasons.most_common()),
        "selected": [
            {
                "language": item.metadata.language_code,
                "id": item.metadata.translation_id,
                "title": item.metadata.title,
                "coverage": item.metadata.coverage,
                "books": item.metadata.total_books,
                "verses": item.metadata.total_verses,
                "license": item.decision.normalized,
                "reason": item.decision.reason,
            }
            for item in result.selected
        ],
    }
