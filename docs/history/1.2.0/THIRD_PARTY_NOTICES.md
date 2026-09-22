# Third-party notices

Application code uses the MIT license in LICENSE. This does not license Bible translations.

No complete Bible text is bundled in1.2.0. The importer accesses BibleNLP/ebible at the pinned revision in data/source_snapshot.json. Every edition retains its own rights notice, source URL, license URL and measured provenance. Only explicit public-domain/CC0/CC BY/CC BY-SA metadata is admitted by the runtime policy. Original source terms prevail; no generic repository license overrides them.

Source format documentation: https://github.com/BibleNLP/ebible . Telegram contracts: https://core.telegram.org/bots/api and https://core.telegram.org/bots/faq . Library documentation: https://docs.aiogram.dev/ . Accessed21September2026 via browser/connector; full source files were not downloaded into this release environment.

Python dependencies are listed in requirements.txt and requirements-test.txt, with their respective upstream licenses. They are installed from package repositories during Docker build, not copied into this source ZIP. The resolved tree is captured in BUILD-DEPENDENCIES.txt inside the built image and runtime-evidence/dependencies.txt on the VPS. Container base images and OS packages have their own licenses. This ZIP does not redistribute their binaries.

UI catalogs are translations prepared for this project; independent native-speaker review has not been performed. Synthetic test fixtures are marked as synthetic and are not Scripture. Historical reports are explicitly not evidence for current live functionality.
