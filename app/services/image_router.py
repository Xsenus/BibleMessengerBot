"""Durable failover with daily provider limits and one monthly paid-request budget."""

from __future__ import annotations

import hashlib
import json
import httpx
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from app.services import artwork, bible, illustrations, image_quality, visual_scene
from app.services import image_providers as api
from app.services.locks import lock_key


def providers(settings):
    from app.services.openai_credentials import image_keys
    keys = {"openai": settings.key, **settings.extra_keys}
    return [
        api.Provider(name, key) for name in settings.provider_order
        for key in (image_keys(settings.key, settings.openai_keys) if name == 'openai' else (keys.get(name, ''),))
        if key
    ]


def attempt_limit(settings):
    return max(5, len(providers(settings)))


def fingerprint(provider):
    return hashlib.sha256(provider.key.encode()).hexdigest()


async def configure(connection, settings):
    """New keys get independent breakers; existing cooldowns/history are retained."""
    for provider in providers(settings):
        await connection.execute(
            """INSERT INTO image_provider_health(provider,key_fingerprint)
            VALUES($1,$2) ON CONFLICT(provider,key_fingerprint) DO NOTHING""",
            provider.name,
            fingerprint(provider),
        )
    # A legacy auth failure is safe to retry with a newly available alternative.
    # Legacy uncertain and persistence-uncertain outcomes remain untouched.
    if providers(settings):
        await connection.execute("""UPDATE image_generation_jobs SET state='retry',retry_at=NULL
            WHERE state='failed' AND error_code IN ('auth','missing_key','quota','providers_exhausted')
            AND attempts<$1""", attempt_limit(settings))


async def reserve(connection, job_id, settings):
    """Select provider and reserve budget atomically across jobs/processes."""
    async with connection.transaction():
        await connection.execute("SELECT pg_advisory_xact_lock($1)", lock_key("image-budget", 1))
        job = await connection.fetchrow(
            "SELECT * FROM image_generation_jobs WHERE id=$1 FOR UPDATE", job_id
        )
        if not job or job["state"] not in {"queued", "retry"} or job["attempts"] >= attempt_limit(settings):
            return None, None, "budget_or_not_due"
        if job["retry_at"] and job["retry_at"] > datetime.now(UTC):
            return None, None, "budget_or_not_due"
        used = await connection.fetch(
            "SELECT provider,key_fingerprint FROM image_generation_attempts WHERE job_id=$1", job_id
        )
        health = {
            (r["provider"], r["key_fingerprint"]): r for r in await connection.fetch("SELECT * FROM image_provider_health")
        }
        usage = await connection.fetch("""SELECT provider,key_fingerprint,COALESCE(sum(reserved_usd),0) AS monthly,
            count(*) FILTER(WHERE created_at >= (date_trunc('day',now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC')) AS daily
            FROM image_generation_attempts
            WHERE created_at >= (date_trunc('month',now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC')
            GROUP BY provider,key_fingerprint""")
        monthly_reserved = sum(r["monthly"] for r in usage)
        daily_used = {}
        credential_used = {}
        for record in usage:
            daily_used[record['provider']] = daily_used.get(record['provider'], 0) + record['daily']
            credential_used[(record['provider'], record['key_fingerprint'])] = record['daily']
        candidates = []
        blocked = False
        daily_exhausted = False
        for provider in providers(settings):
            if any(
                r["provider"] == provider.name
                and r["key_fingerprint"] in {None, fingerprint(provider)}
                for r in used
            ):
                continue
            identifier = (provider.name, fingerprint(provider))
            daily_count = (credential_used.get(identifier, 0) + credential_used.get(('openai', None), 0) if provider.name == 'openai'
                           and settings.openai_daily_scope == 'key' else daily_used.get(provider.name, 0))
            if daily_count >= settings.daily_requests:
                daily_exhausted = True
                continue
            state = health.get(identifier)
            if (
                state
                and state["key_fingerprint"] == fingerprint(provider)
                and state["blocked_until"]
                and state["blocked_until"] > datetime.now(UTC)
            ):
                blocked = True
                continue
            candidates.append(provider)
        # Preserve provider order, balancing usage only between keys of the same service.
        candidates.sort(key=lambda p: (settings.provider_order.index(p.name),
                                      credential_used.get((p.name, fingerprint(p)), 0)))
        if not candidates:
            if daily_exhausted:
                # Keep the job eligible for the next UTC day; another provider
                # can also become available without resetting any paid history.
                return None, None, "budget_or_not_due"
            if blocked or not providers(settings):
                await connection.execute(
                    "UPDATE image_generation_jobs SET state='retry',retry_at=now()+interval '1 minute' WHERE id=$1",
                    job_id,
                )
                return None, None, "providers_unavailable"
            await connection.execute(
                "UPDATE image_generation_jobs SET state='failed',error_code='providers_exhausted',updated_at=now() WHERE id=$1",
                job_id,
            )
            return None, None, "providers_exhausted"
        provider = next(
            (
                p
                for p in candidates
                if monthly_reserved + p.reservation_usd <= settings.monthly_usd
            ),
            None,
        )
        if not provider:
            return None, None, "budget_or_not_due"
        attempt = await connection.fetchval(
            """INSERT INTO image_generation_attempts
            (job_id,reserved_usd,provider,model,key_fingerprint) VALUES($1,$2,$3,$4,$5) RETURNING id""",
            job_id,
            provider.reservation_usd,
            provider.name,
            provider.model,
            fingerprint(provider),
        )
        await connection.execute(
            """UPDATE image_generation_jobs SET state='running',attempts=attempts+1,
            model=$2,retry_at=NULL,updated_at=now() WHERE id=$1""",
            job_id,
            provider.model,
        )
        return attempt, provider, None


async def fail(connection, job_id, attempt, provider, error, *, max_attempts=5):
    async with connection.transaction():
        await connection.execute(
            "UPDATE image_generation_attempts SET state=$2,error_code=$3 WHERE id=$1",
            attempt,
            "uncertain" if error.uncertain else "failed",
            error.code,
        )
        # A moderation rejection is terminal for the source, not routed around.
        terminal = error.code == "moderation" or await connection.fetchval(
            "SELECT attempts>=$2 FROM image_generation_jobs WHERE id=$1", job_id, max_attempts
        )
        await connection.execute(
            """UPDATE image_generation_jobs SET state=$2,error_code=$3,
            retry_at=now(),updated_at=now() WHERE id=$1""",
            job_id,
            "failed" if terminal else "retry",
            error.code,
        )
        if error.code not in {"moderation", "rejected", "remote_failed"}:
            seconds = 86400 if error.code in {"auth", "quota"} else max(60, error.retry_after)
            await connection.execute(
                """INSERT INTO image_provider_health
                (provider,key_fingerprint,failures,blocked_until,error_code) VALUES($1,$2,1,$3,$4)
                ON CONFLICT(provider,key_fingerprint) DO UPDATE SET failures=image_provider_health.failures+1,
                blocked_until=EXCLUDED.blocked_until,error_code=EXCLUDED.error_code,updated_at=now()""",
                provider.name,
                fingerprint(provider),
                datetime.now(UTC) + timedelta(seconds=seconds),
                error.code,
            )


async def recover(connection):
    """Resume accepted async tasks after restart, quarantine submissions without IDs."""
    async with connection.transaction():
        await connection.execute("""UPDATE image_generation_jobs j SET state='retry',retry_at=now(),
            error_code=NULL,updated_at=now() WHERE state='running' AND EXISTS
            (SELECT 1 FROM image_generation_attempts a WHERE a.job_id=j.id AND a.state='reserved' AND a.polling_url IS NOT NULL)""")
        await connection.execute(
            "UPDATE image_generation_jobs SET state='uncertain',error_code='worker_interrupted',updated_at=now() WHERE state='running'"
        )
        await connection.execute(
            "UPDATE image_generation_attempts SET state='uncertain' WHERE state='reserved' AND polling_url IS NULL"
        )


async def process_job(connection, job_id, settings, *, generator=None):
    job = await connection.fetchrow(
        """SELECT j.*,i.translation_id,i.book_code,i.chapter,i.verse,
        i.text_sha256,i.artwork_scope,i.status AS image_status FROM image_generation_jobs j
        JOIN verse_illustrations i ON i.id=j.image_id WHERE j.id=$1""",
        job_id,
    )
    if not job or job["state"] not in {"queued", "retry"}:
        return "budget_or_not_due"
    if job["retry_at"] and job["retry_at"] > datetime.now(UTC):
        return "budget_or_not_due"
    if job["image_status"] == "ready":
        await connection.execute(
            "UPDATE image_generation_jobs SET state='ready',updated_at=now() WHERE id=$1", job_id
        )
        return "cached"
    edition = await bible.find_translation(connection, job["translation_id"])
    row = await illustrations.source_row(connection, job)
    if not row or not edition or illustrations.identity(row, edition)[4] != job["text_sha256"]:
        await connection.execute(
            "UPDATE image_generation_jobs SET state='failed',error_code='source_changed',updated_at=now() WHERE id=$1",
            job_id,
        )
        return "source_changed"
    pending = await connection.fetchrow(
        "SELECT * FROM image_generation_attempts WHERE job_id=$1 AND state='reserved' AND polling_url IS NOT NULL ORDER BY id DESC LIMIT 1",
        job_id,
    )
    resume = None
    scene = None
    if not generator and not pending and any(p.name == 'stability' for p in providers(settings)):
        try:
            stored_usage=json.loads(job['usage']) if isinstance(job['usage'],str) else job['usage'] or {}
            scene=stored_usage.get('scene_description')
            if not scene:
                scene = await visual_scene.describe(job['prompt'])
                await connection.execute("UPDATE image_generation_jobs SET usage=$2::jsonb WHERE id=$1",job_id,json.dumps(dict(stored_usage,scene_description=scene)))
        except (ValueError, KeyError, httpx.HTTPError, TimeoutError):
            # No paid reservation when the free planner cannot prepare a scene.
            settings = replace(settings, provider_order=tuple(p for p in settings.provider_order if p != 'stability'))
    if pending:
        provider = next((p for p in providers(settings) if p.name == pending["provider"]
                         and fingerprint(p) == pending['key_fingerprint']), None)
        if not provider:
            return "providers_unavailable"
        attempt = pending["id"]
        resume = {"id": pending["request_id"], "polling_url": pending["polling_url"]}
        if pending["created_at"] < datetime.now(UTC) - timedelta(minutes=15):
            await fail(
                connection,
                job_id,
                attempt,
                provider,
                api.GenerationError("remote_timeout", uncertain=True),
                max_attempts=attempt_limit(settings),
            )
            return "remote_timeout"
    else:
        attempt, provider, reason = await reserve(connection, job_id, settings)
        if not attempt:
            return reason

    async def accepted(identifier, url):
        await connection.execute(
            "UPDATE image_generation_attempts SET request_id=$2,polling_url=$3 WHERE id=$1",
            attempt,
            identifier,
            url,
        )

    try:
        if generator:
            data, usage, request_id = await generator(
                provider, scene if provider.name == 'stability' and scene else job["prompt"], accepted=accepted, resume=resume
            )
        elif provider.name == "openai":
            data, usage, request_id = await artwork.generate(
                replace(settings, key=provider.key), job["prompt"]
            )
        else:
            data, usage, request_id = await api.generate(
                provider, scene if provider.name == 'stability' and scene else job["prompt"], accepted=accepted, resume=resume
            )
        if provider.name == 'stability' and scene:
            usage=dict(usage,scene_description=scene)
        # The source may have changed while waiting for the external API.
        current = await illustrations.source_row(connection, job)
        if not current or illustrations.identity(current, edition)[4] != job["text_sha256"]:
            await connection.execute(
                "UPDATE image_generation_attempts SET state='succeeded',request_id=$2 WHERE id=$1",
                attempt,
                request_id,
            )
            await connection.execute(
                "UPDATE image_generation_jobs SET state='failed',error_code='source_changed',updated_at=now() WHERE id=$1",
                job_id,
            )
            return "source_changed"
        await connection.execute('UPDATE image_generation_attempts SET request_id=$2 WHERE id=$1',attempt,request_id)
        verdict = await image_quality.gate(connection, job['image_id'], attempt, data)
        if verdict == 'rejected':
            await fail(connection, job_id, attempt, provider, api.GenerationError('quality_text'), max_attempts=attempt_limit(settings))
            return 'quality_text'
        if verdict == 'unavailable':
            await connection.execute("UPDATE image_generation_attempts SET state='succeeded',request_id=$2,error_code='quality_unavailable' WHERE id=$1", attempt, request_id)
            await connection.execute("UPDATE image_generation_jobs SET state='failed',error_code='quality_unavailable',updated_at=now() WHERE id=$1", job_id)
            return 'quality_unavailable'
        async with connection.transaction():
            await illustrations.store(
                connection,
                current,
                edition,
                data,
                job["prompt"],
                image_id=job["image_id"],
                prompt_version=job["prompt_version"],
            )
            await connection.execute(
                """UPDATE image_generation_jobs SET state='ready',usage=$2::jsonb,
                request_id=$3,error_code=NULL,updated_at=now() WHERE id=$1""",
                job_id,
                json.dumps(usage),
                request_id,
            )
            await connection.execute(
                "UPDATE image_generation_attempts SET state='succeeded',request_id=$2 WHERE id=$1",
                attempt,
                request_id,
            )
            await connection.execute(
                "UPDATE image_provider_health SET failures=0,blocked_until=NULL,error_code=NULL WHERE provider=$1 AND key_fingerprint=$2",
                provider.name, fingerprint(provider),
            )
        return "ready"
    except api.GenerationError as error:
        persisted = await connection.fetchrow(
            "SELECT * FROM image_generation_attempts WHERE id=$1", attempt
        )
        if persisted["polling_url"] and (error.code == "remote_pending" or error.uncertain):
            await connection.execute(
                "UPDATE image_generation_jobs SET state='retry',retry_at=now()+interval '30 seconds',error_code='remote_pending',updated_at=now() WHERE id=$1",
                job_id,
            )
            return "remote_pending"
        await fail(connection, job_id, attempt, provider, error, max_attempts=attempt_limit(settings))
        return error.code
    except Exception:
        # Failed persistence must never trigger another paid request.
        async with connection.transaction():
            await connection.execute(
                "UPDATE image_generation_attempts SET state='uncertain',error_code='persistence_uncertain' WHERE id=$1",
                attempt,
            )
            await connection.execute(
                "UPDATE image_generation_jobs SET state='uncertain',error_code='persistence_uncertain',updated_at=now() WHERE id=$1",
                job_id,
            )
        raise
