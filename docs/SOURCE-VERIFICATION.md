# Проверка источников

Три адаптера используют собственные документированные каталоги и форматы. Доступность сайта и корректность отдельного файла проверяются при конкретном запуске; успешный synthetic HTTP-тест не подтверждает внешний сервис.

## Первичные ресурсы

- [BibleNLP/ebible](https://github.com/BibleNLP/ebible): выравнивание verse-per-line, reference list и сведения об изданиях. На запуск закрепляется один неизменяемый Git commit.
- [Каталог getBible/v2](https://api.getbible.net/v2/translations.json): имена, языки, лицензии, URL и publisher SHA изданий.
- [Коды книг getBible](https://github.com/getbible/v2_builder/blob/master/conf/bookNumbers.json) и [USFM Book Identifiers](https://ubsicap.github.io/usfm/identification/books.html): основание для соответствия номеров книг, в том числе дополнительных.
- [HelloAO simplified format](https://bible.helloao.org/docs/reference/translations/simplified.html): контракт полного JSON-текста.
- [eBible: пример russyn](https://ebible.org/find/show.php?id=russyn) и [права WEB](https://ebible.org/engwebp/copyright.htm): условия конкретных изданий, которые нельзя переносить на другие тексты по сходству названия.
- [Berean licensing](https://berean.bible/licensing.htm): первичное заявление для специального правила BSB.

## Сохраняемые доказательства

Кэш содержит исходные байты и SHA-256; отчёт — ресурс, URL, ревизию, идентификатор, лицензионное решение, результаты структуры и записи. getBible publisher SHA-1 проверяется как хеш исходного JSON, отдельно от локального SHA-256. Содержательный SHA-256 строится по нормализованным координатам/тексту и повторно сверяется по строкам PostgreSQL.

BibleNLP `latest` сначала разрешается в commit; при проблеме разрешения предусмотрен явно отмеченный fallback на известную ревизию. Явно заданный commit не меняется из-за `--refresh`. Native-источники сохраняют свою нумерацию; противоречия каталога/содержания не исправляются догадкой или смешением переводов.

Издания `getbible:synodal` и `getbible:web` реально скачаны и импортированы в ходе проверки 2026-09-22. Точные количества и хеши приведены в [VALIDATION.md](VALIDATION.md). Остальные кандидаты каталога не объявляются проверенными только потому, что их обнаружил `discover`. Неудавшийся вариант BibleNLP `russyn` не используется как замена проверенному getBible-корпусу: его структурную несовместимость следует разбирать по отдельному отчёту источника.

Лицензионный фильтр консервативен и сохраняет причины отказа. Программная лицензия репозитория не лицензирует каждый включённый текст; см. [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md).
