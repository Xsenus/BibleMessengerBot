# Changelog

## 1.1.0 — 2026-09-21

Rebuilt release because the previously referenced 1.0.0 artifact was not
present in the working storage and therefore could not be safely patched.

- Added an eBible/BibleNLP catalog importer with per-edition license checks.
- Added `core`, `extended`, and `all-open` import profiles; core/extended import up to two open editions per language by default.
- Added strict default policy: Public Domain, CC0, CC BY, and CC BY-SA only.
- Added multilingual PostgreSQL schema with source provenance and SHA-256.
- Added idempotent, transactional bulk import and resumable download cache.
- Added Russian, English, Ukrainian, Spanish, French, German, Portuguese,
  Italian, Polish, Romanian, Chinese, Arabic, Hebrew, Hindi and many other
  language priorities.
- Added Telegram private-chat, group, and channel registration workflows.
- Added sequential reading, verse of the day, random verse, reading plans,
  scheduling, delivery logs, rate limiting, and retries.
- Added FastAPI health/admin endpoints, Docker Compose, one-command installer,
  backup, restore, diagnostics, tests, and release audit tooling.
