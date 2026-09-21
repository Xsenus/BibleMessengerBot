from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_required_files_exist():
    required = [
        "Dockerfile",
        "docker-compose.yml",
        ".env.example",
        "install.sh",
        "sql/schema.sql",
        "data/books.json",
        "data/language_profiles.json",
        "app/bot/main.py",
        "app/worker/main.py",
        "app/web/main.py",
    ]
    assert all((ROOT / item).is_file() for item in required)


def test_compose_shape():
    data = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    assert {"postgres", "bootstrap", "bot", "worker", "admin"}.issubset(data["services"])
    assert data["services"]["admin"]["ports"]


def test_no_real_bot_token_in_example():
    text = (ROOT / ".env.example").read_text()
    assert "BOT_TOKEN=replace_me" in text
    assert not any(line.startswith("BOT_TOKEN=") and ":" in line for line in text.splitlines())


def test_default_profile_imports_up_to_two_editions_per_language():
    example = (ROOT / ".env.example").read_text()
    installer = (ROOT / "install.sh").read_text()
    assert "BIBLE_PROFILE=extended" in example
    assert "MAX_EDITIONS_PER_LANGUAGE=2" in example
    assert 'MAX_EDITIONS="${MAX_EDITIONS_PER_LANGUAGE:-2}"' in installer
    assert "MAX_EDITIONS_PER_LANGUAGE=${MAX_EDITIONS}" in installer
