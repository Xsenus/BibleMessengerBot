"""One atomic edition write; source aliases never overwrite subscriptions or merge approximate texts."""
from __future__ import annotations
import hashlib
import json
from dataclasses import asdict
from typing import Any

from app.catalog.audit import OT, NT
from app.catalog.importer import import_translation
from app.catalog.multisource.types import Prepared
from app.catalog.policy import decide_license
from app.services.locks import lock_key


class UpdateHeld(RuntimeError):
    """A changed upstream edition is not silently substituted for a reader's selected text."""


async def database_fingerprint(connection: Any, translation_id: int, numbering: str) -> str:
    h=hashlib.sha256();h.update(('BibleMessenger-content-v1\n'+numbering+'\n').encode())
    query='''SELECT book_code,chapter,verse,text,is_range_continuation FROM verses
        WHERE translation_id=$1
        ORDER BY COALESCE(array_position($2::text[],book_code)-1,1000),book_code,chapter,verse'''
    async for row in connection.cursor(query,translation_id,OT+NT,prefetch=2000):
        values=[row['book_code'],row['chapter'],row['verse'],row['text'],row['is_range_continuation']]
        h.update((json.dumps(values,ensure_ascii=False,separators=(',',':'))+'\n').encode())
    return h.hexdigest()


async def record_source(connection: Any, prepared: Prepared, translation_id: int) -> None:
    candidate=prepared.candidate
    evidence={**candidate.evidence,'license':asdict(candidate.metadata.license) if candidate.metadata.license else None,
        'decision':asdict(decide_license(candidate.metadata)),'copyright_notice':candidate.metadata.copyright_notice}
    await connection.execute('''INSERT INTO translation_sources(source_slug,source_translation_id,translation_id,
        source_name,source_url,source_sha256,source_revision,content_sha256,numbering_system,license_evidence,metadata)
        VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb,$11::jsonb)
        ON CONFLICT(source_slug,source_translation_id) DO UPDATE SET
        translation_id=EXCLUDED.translation_id,source_url=EXCLUDED.source_url,source_sha256=EXCLUDED.source_sha256,
        source_revision=EXCLUDED.source_revision,content_sha256=EXCLUDED.content_sha256,
        numbering_system=EXCLUDED.numbering_system,license_evidence=EXCLUDED.license_evidence,
        metadata=EXCLUDED.metadata,verified_at=now()''',candidate.source,candidate.metadata.translation_id,
        translation_id,candidate.source_name,candidate.url,prepared.downloaded.sha256,candidate.revision,
        prepared.content_sha256,candidate.numbering,json.dumps(evidence,ensure_ascii=False),
        json.dumps({'title':candidate.metadata.title,'short_title':candidate.metadata.short_title,
            'language':candidate.metadata.language_code,'raw':candidate.raw,'diagnostics':prepared.diagnostics},ensure_ascii=False))


class PostgresStore:
    def __init__(self,connection: Any,*,batch_size: int=2000,allow_replace: bool=False):
        self.connection=connection;self.batch_size=batch_size;self.allow_replace=allow_replace

    async def save(self,prepared: Prepared) -> dict[str,Any]:
        c=self.connection;m=prepared.candidate.metadata;candidate=prepared.candidate
        decision=decide_license(m)
        if not decision.allowed:raise ValueError('License was rejected at persistence boundary')
        async with c.transaction():
            await c.execute('SELECT pg_advisory_xact_lock($1)',lock_key('multi-edition',candidate.key))
            old=await c.fetchrow('''SELECT t.* FROM translations t WHERE t.id=COALESCE(
                (SELECT translation_id FROM translation_sources WHERE source_slug=$1 AND source_translation_id=$2),
                (SELECT id FROM translations WHERE source_name=$3 AND source_translation_id=$2))''',
                candidate.source,m.translation_id,candidate.source_name)
            if old:
                previous=await database_fingerprint(c,old['id'],candidate.numbering)
                if previous==prepared.content_sha256:
                    if old['license_type'] != decision.normalized:
                        raise UpdateHeld('License declaration changed; keep previous provenance for manual review')
                    # Keep deliberate deactivation and reading state intact; no implicit re-enable.
                    await c.execute('UPDATE translations SET content_sha256=$2,numbering_system=$3 WHERE id=$1',
                        old['id'],prepared.content_sha256,candidate.numbering)
                    await record_source(c,prepared,old['id'])
                    return {'outcome':'already_present','database_id':old['id']}
                if not self.allow_replace:
                    raise UpdateHeld('Upstream text changed; previous edition preserved. Review then use --replace-existing during maintenance')
                # Replacing an alias must not mutate a different source's canonical edition.
                if old['source_name']!=candidate.source_name or old['source_translation_id']!=m.translation_id:
                    raise UpdateHeld('Changed mirror alias is linked to another edition; automatic replacement is blocked')
                used=await c.fetchval('''SELECT EXISTS(SELECT 1 FROM subscriptions WHERE translation_id=$1)
                    OR EXISTS(SELECT 1 FROM reading_progress WHERE translation_id=$1)
                    OR EXISTS(SELECT 1 FROM chat_reading_progress WHERE translation_id=$1)
                    OR EXISTS(SELECT 1 FROM delivery_log WHERE translation_id=$1)
                    OR EXISTS(SELECT 1 FROM telegram_chats WHERE default_translation_id=$1)
                    OR EXISTS(SELECT 1 FROM telegram_users WHERE default_translation_id=$1)
                    OR EXISTS(SELECT 1 FROM translation_sources WHERE translation_id=$1 AND (source_slug<>$2 OR source_translation_id<>$3))''',
                    old['id'],candidate.source,m.translation_id)
                if used:raise UpdateHeld('Edition is referenced by subscriptions, progress, defaults or mirrors; replacement blocked')
            else:
                duplicate=await c.fetchrow('''SELECT t.id FROM translations t JOIN languages l ON l.id=t.language_id
                    WHERE l.code=$1 AND t.content_sha256=$2 AND t.numbering_system=$3 AND t.license_type=$4
                    AND t.is_active=true AND ($4 IN ('public-domain','cc0') OR (
                        t.copyright_notice=$5 AND t.copyright_holder=$6 AND t.license_url=$7
                        AND t.translated_by=$8 AND t.copyright_years=$9))
                    ORDER BY t.id LIMIT 1''',m.language_code,prepared.content_sha256,candidate.numbering,decision.normalized,
                    m.copyright_notice,m.license.copyright_holder if m.license else '',
                    m.license.license_url if m.license else '',m.license.translated_by if m.license else '',
                    m.license.copyright_years if m.license else '')
                if duplicate and await database_fingerprint(c,duplicate['id'],candidate.numbering)==prepared.content_sha256:
                    await record_source(c,prepared,duplicate['id'])
                    return {'outcome':'exact_duplicate','database_id':duplicate['id']}
            result=await import_translation(c,prepared.downloaded,prepared.references,batch_size=self.batch_size,
                source_name=candidate.source_name,source_revision=candidate.revision,
                prepared_records=prepared.records,prepared_audit=prepared.audit)
            identifier=result['database_id']
            await c.execute('UPDATE translations SET content_sha256=$2,numbering_system=$3 WHERE id=$1',
                identifier,prepared.content_sha256,candidate.numbering)
            await c.execute('DELETE FROM translation_book_names WHERE translation_id=$1',identifier)
            if prepared.book_names:
                await c.executemany('INSERT INTO translation_book_names(translation_id,book_code,name) VALUES($1,$2,$3)',
                    [(identifier,code,name) for code,name in prepared.book_names.items()])
            # SQL and file checks are independent: verify the rows actually written, before commit.
            if await database_fingerprint(c,identifier,candidate.numbering)!=prepared.content_sha256:
                raise RuntimeError('PostgreSQL content fingerprint differs after COPY; transaction rolled back')
            await record_source(c,prepared,identifier)
            return {'outcome':'updated' if old else 'imported','database_id':identifier}
