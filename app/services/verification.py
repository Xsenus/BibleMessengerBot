"""Actual-database verification; never substitute the planned language profile for counts."""
from __future__ import annotations
import json
import os
from typing import Any
from app.catalog.profiles import normalize_language_code,profile_languages


def required_languages() -> set[str]:
    """An operator can change the explicit bootstrap gate; Russian and English by default."""
    return {normalize_language_code(x.strip()) for x in os.getenv('REQUIRED_LANGUAGES','rus,eng').split(',') if x.strip()}


async def verify_database(connection: Any, *, profile: str = 'extended', full: bool = True) -> dict[str,Any]:
    """Check imported rows, dense indexes, chapter counts and audit provenance."""
    rows = await connection.fetch('''SELECT t.*,l.code AS language_code FROM translations t
        JOIN languages l ON l.id=t.language_id WHERE t.is_active ORDER BY l.code,t.source_translation_id''')
    errors,editions = [],[]
    for row in rows:
        item = {'id':row['source_translation_id'],'language':row['language_code'],'coverage':row['coverage'],
            'audit_status':row['audit_status'],'books':row['book_count'],'verse_rows':row['verse_count'],
            'visible_verses':row['nonempty_verse_count'],'sha256':row['source_sha256'],
            'reference_sha256_normalized':row['reference_sha256'],'source_revision':row['source_revision'],
            'canonical_66_structural':row['canonical_66_complete'],'nt_structural':row['nt_complete']}
        if row['audit_status'] not in {'passed','passed_with_warnings'} or not row['source_revision'] or not row['reference_sha256']:
            errors.append(f"{item['id']}: not audited")
        if full:
            physical = await connection.fetchrow('''SELECT count(*) AS rows,
                count(*) FILTER(WHERE text<>'' AND NOT is_range_continuation) AS visible,
                count(DISTINCT book_code) FILTER(WHERE text<>'' AND NOT is_range_continuation) AS books,
                min(ordinal) AS ordinal_min,max(ordinal) AS ordinal_max,count(ordinal) AS ordinal_count,
                count(DISTINCT (book_code,chapter)) FILTER(WHERE text<>'' AND NOT is_range_continuation) AS chapters
                FROM verses WHERE translation_id=$1''',row['id'])
            chapters = await connection.fetchrow('SELECT count(*) AS n,min(position) AS first,max(position) AS last,sum(verse_count) AS visible FROM translation_chapters WHERE translation_id=$1',row['id'])
            if (physical['rows']!=row['verse_count'] or physical['visible']!=row['nonempty_verse_count'] or
                physical['books']!=row['book_count'] or physical['ordinal_count']!=physical['visible'] or
                physical['ordinal_min']!=1 or physical['ordinal_max']!=physical['visible'] or
                chapters['n']!=physical['chapters'] or chapters['first']!=1 or chapters['last']!=chapters['n'] or
                chapters['visible']!=physical['visible']):
                errors.append(f"{item['id']}: physical counts/index do not match metadata")
            invalid = await connection.fetchval('''SELECT count(*) FROM verses WHERE translation_id=$1 AND
                ((ordinal IS NOT NULL AND (text='' OR is_range_continuation)) OR
                 (text<>'' AND NOT is_range_continuation AND (ordinal IS NULL OR verse_end<verse OR verse_end IS NULL)))''',row['id'])
            if invalid:
                errors.append(f"{item['id']}: invalid verse/range indexes")
            item['physical'] = dict(physical)
            item['chapter_index'] = dict(chapters)
        report = json.loads(row['validation_report']) if isinstance(row['validation_report'],str) else row['validation_report']
        item['structural_audit'] = report
        editions.append(item)
    languages = sorted({r['language_code'] for r in rows if r['audit_status'] in {'passed','passed_with_warnings'} and r['nonempty_verse_count']>0})
    full_languages = {r['language_code'] for r in rows if r['canonical_66_complete'] and r['audit_status'] in {'passed','passed_with_warnings'}}
    missing_required = sorted(required_languages()-full_languages) if profile!='none' else []
    if missing_required:
        errors.append('Required structurally complete core editions missing: '+','.join(missing_required))
    if not editions and profile!='none':
        errors.append('No editions physically imported')
    wanted = profile_languages(profile)
    return {'status':'passed' if not errors else 'failed','scope':'actual PostgreSQL rows' if full else 'metadata readiness only',
        'languages':languages,'language_count':len(languages),'edition_count':len(editions),'editions':editions,
        'profile':profile,'missing_requested_languages':sorted(set(wanted or [])-set(languages)),
        'missing_required_full_languages':missing_required,'errors':errors,
        'warning':'Structural completeness is not native-versification or independent textual proofreading.'}
