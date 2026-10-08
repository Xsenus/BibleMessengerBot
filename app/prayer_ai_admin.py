"""Prepare pinned, verified free local prayer model; never execute remote code."""
import asyncio
import hashlib
import json
import os
from pathlib import Path

import httpx

MODEL=json.loads((Path(__file__).parent/'data/prayer-model.json').read_text())


async def prepare(*, client=None):
    root=Path(os.getenv('PRAYER_MODELS_PATH','/app/cache/prayer-ai'))
    root.mkdir(parents=True,exist_ok=True)
    target=root/MODEL['name']
    if target.is_file() and target.stat().st_size==MODEL['bytes']:
        with target.open('rb') as stream:
            if hashlib.file_digest(stream,'sha256').hexdigest()==MODEL['sha256']:
                return
    temporary=target.with_suffix('.part')
    owned=client is None
    client=client or httpx.AsyncClient(timeout=180,follow_redirects=True)
    try:
        url=f"https://huggingface.co/{MODEL['repository']}/resolve/{MODEL['revision']}/{MODEL['name']}"
        digest=hashlib.sha256()
        size=0
        async with client.stream('GET',url) as response:
            response.raise_for_status()
            with temporary.open('wb') as stream:
                async for block in response.aiter_bytes():
                    size+=len(block)
                    if size>MODEL['bytes']:
                        raise ValueError('prayer_model_too_large')
                    digest.update(block)
                    stream.write(block)
        if size!=MODEL['bytes'] or digest.hexdigest()!=MODEL['sha256']:
            raise ValueError('prayer_model_checksum')
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
        if owned:
            await client.aclose()


if __name__=='__main__':
    asyncio.run(prepare())
