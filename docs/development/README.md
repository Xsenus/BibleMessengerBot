# Для разработчиков

| Документ | Содержание |
|---|---|
| [systems.md](systems.md) | Подробное описание каждой системы: как устроена и как работает |
| [new-platform.md](new-platform.md) | Как добавить новый мессенджер (шаг за шагом) |
| [extending.md](extending.md) | Как добавить генератор картинок, источник видео, способ доставки |
| [admin-redesign.md](admin-redesign.md) | Редизайн веб-панели: аудит, правила и план по этапам |
| [../../CONTRIBUTING.md](../../CONTRIBUTING.md) | Правила работы с кодом, проверки перед коммитом |

Быстрые проверки перед коммитом: `ruff check .`, `mypy`, `pytest` (для тестов с БД: `RUN_DB_TESTS=1 DATABASE_URL=postgresql://…`).

## Локальное окружение

Используйте Python 3.12 или новее; production-образ использует Python 3.14, полные тесты CI проверяют Python 3.12 и 3.14. Панель собирается на Node.js 26 в CI и Docker. В Windows создайте `.venv` установленным интерпретатором подходящей версии, например:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -c constraints-linux.txt -r requirements-dev.txt -r requirements-test.txt -r requirements-neural.txt
.\.venv\Scripts\python.exe -m pip check
```

Linux использует те же requirements и закреплённые зависимости. FFmpeg, eSpeak NG и Tesseract устанавливаются отдельно; веса голосов проверяются командой `python -m app.neural_audio_admin prepare`. Не переносите production-токены и не направляйте тесты на production-БД. Тесты с `RUN_DB_TESTS=1` создают и удаляют только собственные уникальные временные базы.

`requirements-neural.txt` задаёт официальный CPU-индекс и точный вариант PyTorch с `+cpu`; отдельная версия Torch в Docker/CI не дублируется. Dependabot объединяет обновления Hub/Transformers/tokenizers в одну группу. Pydantic >=2.14 и mpmath >=1.4 исключены до появления совместимых Aiogram/SymPy; multidict >=7 — до совместимого aiohttp. Версия pydantic-core меняется только вместе с Pydantic, который требует точное совпадение. Torch обновляется вручную с сохранением `+cpu` и проверкой реальных голосов на обеих версиях Python.

`python -m app.cli migrate --check` ничего не меняет; код 3 означает ожидающие миграции. `python -m app.cli migrate` применяет только схему под блокировкой обслуживания, без повторного наполнения справочников. Для первоначальной установки предусмотрены `seed` и bootstrap. Остановите все подключённые обработчики перед миграциями; в production используйте [штатное обновление](../operations/operations.md#обновление-кода).

Результаты тестов, coverage и кэши не входят в Git. Рабочие `.env`, локальные БД, сертификаты, модели голосов и другие нужные для запуска данные сохраняйте отдельно от временных отчётов.
