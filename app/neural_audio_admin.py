"""Install and verify pinned voices; do not download weights during a reading."""
import asyncio
import json
import sys

from app.services import neural_speech


async def main():
    if len(sys.argv) == 2 and sys.argv[1] == 'prepare':
        await neural_speech.prepare()
    report = {code: neural_speech.complete(code) for code in neural_speech.LANGUAGES}
    print(json.dumps(report))
    if not all(report.values()):
        raise SystemExit(1)


if __name__ == '__main__':
    asyncio.run(main())
