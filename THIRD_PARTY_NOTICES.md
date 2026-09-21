# Third-party data notices

BibleMessengerBot does not claim copyright in Bible translations.

The default importer uses the curated BibleNLP/eBible verse corpus and its
metadata. Every imported edition stores:

- upstream translation identifier;
- publication/source URL;
- copyright statement and holder when supplied;
- license type, version, and license URL;
- source file URL and SHA-256 checksum;
- import timestamp and coverage statistics.

The application only imports editions accepted by the configured license
policy. The default policy accepts Public Domain, CC0, CC BY, and CC BY-SA. It
rejects NonCommercial, NoDerivatives, unknown, and non-redistributable
editions. Operators remain responsible for complying with attribution and
share-alike requirements shown in the admin panel and database.

Primary data sources:

- eBible.org — Bible publication and download service.
- BibleNLP/ebible — curated verse-per-line corpus derived from eBible.org.

The software dependencies retain their respective licenses.
