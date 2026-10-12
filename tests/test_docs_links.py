"""Документация: относительные ссылки между файлами Markdown не должны быть битыми."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LINK = re.compile(r"\]\(([^)\s#]+)(?:#[^)\s]*)?\)")


def test_markdown_links_resolve():
    files = [ROOT / name for name in ("README.md", "CHANGELOG.md", "CONTRIBUTING.md", "SECURITY.md")]
    files += [p for p in (ROOT / "docs").rglob("*.md") if "history" not in p.relative_to(ROOT / "docs").parts[:1]]
    broken = []
    for path in files:
        for match in LINK.finditer(path.read_text()):
            target = match.group(1)
            if re.match(r"^[a-z]+:", target):
                continue
            if not (path.parent / target).exists():
                broken.append(f"{path.relative_to(ROOT)} -> {target}")
    assert not broken, broken


def test_every_docs_folder_has_an_index():
    for folder in ("getting-started", "user-guide", "admin", "platforms", "features", "operations", "reference", "development"):
        assert (ROOT / "docs" / folder / "README.md").is_file(), folder
    assert (ROOT / "docs" / "README.md").is_file()
