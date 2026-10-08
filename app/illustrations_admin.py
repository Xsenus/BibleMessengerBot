"""Export queued prompts and import generated images without calling paid APIs."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import asyncpg

from app.config import Settings
from app.db import normalize_asyncpg_dsn
from app.services import artwork, bible, illustrations


def parser():
    root = argparse.ArgumentParser(description="Bible verse illustration queue")
    sub = root.add_subparsers(dest="command", required=True)
    sub.add_parser("stats")
    sub.add_parser("providers")
    export = sub.add_parser("export")
    export.add_argument("--limit", type=int, default=20)
    export.add_argument("--output", type=Path, required=True)
    add = sub.add_parser("add")
    add.add_argument("--translation", required=True)
    add.add_argument("--book", required=True)
    add.add_argument("--chapter", type=int, required=True)
    add.add_argument("--verse", type=int)
    add.add_argument("--scope", choices=['verse','chapter'], default='verse')
    add.add_argument("--file", type=Path, required=True)
    add.add_argument("--prompt-file", type=Path)
    return root


async def run(args):
    c = await asyncpg.connect(
        normalize_asyncpg_dsn(Settings.from_env(require_bot_token=False).database_url)
    )
    try:
        if args.command == 'providers':
            from app.services import image_router, image_providers
            settings = artwork.ArtSettings.from_env()
            configured = {p.name for p in image_router.providers(settings)}
            health = {r['provider']: dict(r) for r in await c.fetch(
                'SELECT provider,failures,blocked_until,error_code FROM image_provider_health')}
            usage = await c.fetch("""SELECT provider,COALESCE(sum(reserved_usd),0) AS monthly,
                count(*) FILTER(WHERE created_at >= (date_trunc('day',now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC')) AS daily
                FROM image_generation_attempts
                WHERE created_at >= (date_trunc('month',now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC')
                GROUP BY provider""")
            daily = {r['provider']: r['daily'] for r in usage}
            return {'mode': settings.provider, 'order': settings.provider_order,
                    'daily_max_requests_per_provider': settings.daily_requests,
                    'monthly_reserved_limit_usd': str(settings.monthly_usd),
                    'monthly_reserved_usd': str(sum(r['monthly'] for r in usage)),
                    'providers': [{'provider': name, 'model': image_providers.MODELS[name],
                                   'daily_requests_used': daily.get(name, 0),
                                   'daily_requests_remaining': max(0, settings.daily_requests - daily.get(name, 0)),
                                   'configured': name in configured, **health.get(name, {})}
                                  for name in settings.provider_order]}
        if args.command == "stats":
            return [
                dict(r)
                for r in await c.fetch("""SELECT status,count(*) AS images,
                COALESCE(sum(octet_length(image_data)),0) AS bytes FROM verse_illustrations GROUP BY status""")
            ]
        if args.command == "export":
            if not 1 <= args.limit <= 1000:
                raise ValueError("Choose 1 to 1000 prompts")
            rows = await c.fetch(
                """SELECT i.id,i.text_sha256,i.translation_id,i.book_code,i.chapter,i.verse,i.artwork_scope,t.title,j.prompt AS prepared_prompt
                FROM verse_illustrations i
                JOIN translations t ON t.id=i.translation_id
                LEFT JOIN image_generation_jobs j ON j.image_id=i.id
                WHERE i.status='pending' ORDER BY i.id LIMIT $1""",
                args.limit,
            )
            verified = []
            for image in rows:
                source = await illustrations.source_row(c,image)
                if source and illustrations.identity(source,{'id':image['translation_id']})[4] == image['text_sha256']:
                    verified.append(dict(image,source_row=source))
            rows = verified
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                "".join(
                    json.dumps(
                        {
                            "id": r["id"],
                            "prompt": r["prepared_prompt"]
                            or illustrations.prompt_for(r['source_row'], r["title"]),
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                    for r in rows
                ),
                encoding="utf-8",
            )
            return {"exported": len(rows)}
        edition = await bible.find_translation(c, args.translation)
        if not edition:
            raise ValueError("Edition not found")
        if args.scope == 'verse' and args.verse is None:
            raise ValueError('--verse is required for verse artwork')
        row = await illustrations.source_row(c,dict(translation_id=edition['id'],book_code=args.book,
            chapter=args.chapter,verse=args.verse,artwork_scope=args.scope))
        if not row:
            raise ValueError('Source not found')
        prompt = (
            args.prompt_file.read_text(encoding="utf-8")
            if args.prompt_file
            else artwork.prompt(
                row,
                edition,
                slot="on_demand",
                context=await artwork.source_context(c, row, edition),
            )
        )
        identifier = await illustrations.store(
            c,
            row,
            edition,
            args.file.read_bytes(),
            prompt,
            prompt_version=(3 if args.scope=='chapter' else 2) if not args.prompt_file else 1,
        )
        return {"image_id": identifier, "status": "ready"}
    finally:
        await c.close()


if __name__ == "__main__":
    print(json.dumps(asyncio.run(run(parser().parse_args())), ensure_ascii=False, default=str))
