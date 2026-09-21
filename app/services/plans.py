"""Deterministic chapter distribution across the requested number of deliveries."""
from __future__ import annotations

PLANS = {'bible-90': (90, None), 'bible-180': (180, None), 'bible-365': (365, None),
         'new-testament-90': (90, 'NT'), 'psalms-150': (150, 'PSA')}


def plan_window(total_chapters: int, duration: int, completed_days: int) -> tuple[int, int]:
    """Return a zero-based half-open chapter range; no overlaps or lost chapters."""
    if total_chapters < 1 or duration < 1 or completed_days < 0:
        raise ValueError('Invalid plan counters')
    if total_chapters < duration:
        raise ValueError('The edition has fewer chapters than plan days')
    if completed_days >= duration:
        return total_chapters, total_chapters
    return (completed_days * total_chapters // duration,
            (completed_days + 1) * total_chapters // duration)
