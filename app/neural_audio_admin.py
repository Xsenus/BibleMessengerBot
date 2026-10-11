"""Установка и проверка закреплённых нейроголосов; веса не скачиваются во время работы бота."""
import asyncio
import json
import sys

from app.services import neural_speech,russian_speech


async def main():
    """При аргументе `prepare` скачать и проверить голоса, затем напечатать отчёт о полноте моделей по языкам; код возврата 1, если чего-то не хватает."""
    if len(sys.argv) == 2 and sys.argv[1] == 'prepare':
        await neural_speech.prepare()
        await russian_speech.prepare()
    report = {code: neural_speech.complete(code) for code in neural_speech.LANGUAGES}
    report['rus_primary']=russian_speech.complete(verify=True)
    print(json.dumps(report))
    if not all(report.values()):
        raise SystemExit(1)


if __name__ == '__main__':
    asyncio.run(main())
