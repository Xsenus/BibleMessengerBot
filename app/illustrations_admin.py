"""Export queued prompts and import generated images without calling paid APIs."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path

import asyncpg

from app.config import Settings
from app.db import normalize_asyncpg_dsn
from app.services import bible, illustrations


def parser():
    root = argparse.ArgumentParser(description="Bible verse illustration queue")
    sub = root.add_subparsers(dest="command", required=True)
    sub.add_parser("stats")
    export = sub.add_parser("export")
    export.add_argument("--limit", type=int, default=20)
    export.add_argument("--output", type=Path, required=True)
    add = sub.add_parser("add")
    add.add_argument("--translation", required=True)
    add.add_argument("--book", required=True)
    add.add_argument("--chapter", type=int, required=True)
    add.add_argument("--verse", type=int, required=True)
    add.add_argument("--file", type=Path, required=True)
    add.add_argument("--prompt-file", type=Path)
    return root


async def run(args):
    c = await asyncpg.connect(
        normalize_asyncpg_dsn(Settings.from_env(require_bot_token=False).database_url)
    )
    try:
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
                """SELECT i.id,i.text_sha256,v.book_code,v.chapter,v.verse,v.text,t.title
                FROM verse_illustrations i JOIN verses v ON v.translation_id=i.translation_id
                    AND v.book_code=i.book_code AND v.chapter=i.chapter AND v.verse=i.verse
                JOIN translations t ON t.id=i.translation_id
                WHERE i.status='pending' ORDER BY i.id LIMIT $1""",
                args.limit,
            )
            rows = [
                r
                for r in rows
                if hashlib.sha256(r["text"].encode("utf-8")).hexdigest() == r["text_sha256"]
            ]
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                "".join(
                    json.dumps(
                        {"id": r["id"], "prompt": illustrations.prompt_for(r, r["title"])},
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
        row = await c.fetchrow(
            """SELECT book_code,chapter,verse,verse_end,text FROM verses
            WHERE translation_id=$1 AND book_code=$2 AND chapter=$3 AND verse=$4 AND text<>'' AND NOT is_range_continuation""",
            edition["id"],
            args.book,
            args.chapter,
            args.verse,
        )
        if not row:
            raise ValueError("Verse not found")
        prompt = (
            args.prompt_file.read_text(encoding="utf-8")
            if args.prompt_file
            else illustrations.prompt_for(row, bible.display_title(edition))
        )
        identifier = await illustrations.store(c, row, edition, args.file.read_bytes(), prompt)
        return {"image_id": identifier, "status": "ready"}
    finally:
        await c.close()


if __name__ == "__main__":
    print(json.dumps(asyncio.run(run(parser().parse_args())), ensure_ascii=False, default=str))
