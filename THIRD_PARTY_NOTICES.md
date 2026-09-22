# Third-party sources and rights — 1.3.0

The MIT license for this application does not license Bible translations. No complete authentic Bible text or populated database is bundled. Synthetic fixtures are marked as test data and are not represented as Scripture.

## BibleNLP / eBible

https://github.com/BibleNLP/ebible and https://ebible.org/
The corpus README specifies verse-per-line alignment, a reference list, and edition-specific rights. This application obtains metadata and individual files at a resolved immutable repository revision. Individual translation license declarations govern reuse, not a repository software license.

## getBible

https://github.com/getbible/v2 and https://api.getbible.net/v2/translations.json
The adapter reads complete-translation JSON and distribution-license metadata for each edition. It does not infer redistribution permission from the host's generic terms or the code repository license.

## HelloAO / Free Use Bible API

https://bible.helloao.org/docs/reference/translations/simplified.html
The adapter reads complete.simple.json and an independent book inventory. Edition rights are checked against exact eBible identifiers or a narrowly scoped primary Berean declaration at https://berean.bible/licensing.htm. No blanket license is inferred for all hosted translations.

## Policy and attribution

Recognized Public Domain, CC0, CC BY and CC BY-SA may pass the application's conservative policy when metadata is consistent and downloading/redistribution are allowed. Unknown, contradictory, NC and ND declarations do not pass. Passing a parser is not a jurisdiction-specific legal opinion. Raw license evidence, source URLs, hashes, notices and decisions are retained. The bot includes edition/source/license attribution with publications.

Primary-source documentation consulted on 2026-09-22. Full live corpus acquisition was not executed in this preparation environment. Runtime dependencies retain their respective licenses; the installer records the installed package versions. No proprietary binaries, font files, private secrets or commercial Bible dumps are included.
