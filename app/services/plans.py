"""Deterministic chapter distribution across the requested number of deliveries."""
from __future__ import annotations
from typing import Any
from app.services.errors import UserError

PLANS = {'bible-90': (90, None), 'bible-180': (180, None), 'bible-365': (365, None),
         'new-testament-90': (90, 'NT'), 'psalms-150': (150, 'PSA')}


async def validate_plan(connection: Any, translation: Any, plan_code: str) -> None:
    """Reject unavailable plan content before accepting a schedule."""
    if plan_code not in PLANS:
        raise UserError('invalid')
    duration, scope = PLANS[plan_code]
    if ((scope is None and not translation['canonical_66_complete']) or
            (scope == 'NT' and not translation['nt_complete'])):
        raise UserError('invalid', 'The requested plan requires a structurally complete edition')
    chapters = await connection.fetchval('''SELECT count(*) FROM translation_chapters c
        JOIN books b ON b.code=c.book_code WHERE c.translation_id=$1
        AND ($2::text IS NULL OR b.testament=$2 OR c.book_code=$2)''', translation['id'], scope)
    if chapters < duration:
        raise UserError('invalid', 'The selected edition has too few chapters for this plan')


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
