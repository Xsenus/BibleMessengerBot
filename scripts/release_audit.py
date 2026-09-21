#!/usr/bin/env python3
"""Repeatable offline release audit for the source package."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED_PARTS = {".git", "__pycache__", ".pytest_cache", ".ruff_cache", "cache", "backups"}
GENERATED = {"MANIFEST.sha256", "RELEASE-AUDIT.json", "RELEASE-AUDIT.md"}
TOKEN_RE = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b")


def files() -> list[Path]:
    return sorted(
        path
        for path in ROOT.rglob("*")
        if path.is_file()
        and not any(part in EXCLUDED_PARTS for part in path.relative_to(ROOT).parts)
        and path.name not in GENERATED
    )


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run(name: str, command: list[str]) -> dict[str, Any]:
    result = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    return {
        "name": name,
        "command": command,
        "returncode": result.returncode,
        "passed": result.returncode == 0,
        "output": result.stdout[-12000:],
    }


def main() -> int:
    checks: list[dict[str, Any]] = []

    # Structured data validation.
    structured_errors: list[str] = []
    for path in files():
        try:
            if path.suffix == ".json":
                json.loads(path.read_text(encoding="utf-8"))
            elif path.suffix in {".yml", ".yaml"}:
                yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            structured_errors.append(f"{path.relative_to(ROOT)}: {exc}")
    checks.append(
        {
            "name": "json_yaml_parse",
            "passed": not structured_errors,
            "errors": structured_errors,
        }
    )

    # Secret and unsafe file checks.
    secret_hits: list[str] = []
    unsafe_files: list[str] = []
    for path in files():
        relative = str(path.relative_to(ROOT))
        if path.name == ".env" or path.is_symlink():
            unsafe_files.append(relative)
        if path.stat().st_size <= 5 * 1024 * 1024:
            text = path.read_text(encoding="utf-8", errors="ignore")
            if TOKEN_RE.search(text):
                secret_hits.append(relative)
    checks.append(
        {
            "name": "secret_scan",
            "passed": not secret_hits and not unsafe_files,
            "token_hits": secret_hits,
            "unsafe_files": unsafe_files,
        }
    )

    checks.extend(
        [
            run("compileall", [sys.executable, "-m", "compileall", "-q", "app", "tests"]),
            run("pytest", [sys.executable, "-m", "pytest", "-q"]),
            run(
                "bash_syntax",
                [
                    "bash",
                    "-n",
                    "install.sh",
                    "update.sh",
                    "backup.sh",
                    "restore.sh",
                    "diagnose.sh",
                ],
            ),
        ]
    )

    required = [
        "README.md",
        "VERSION",
        "Dockerfile",
        "docker-compose.yml",
        ".env.example",
        "sql/schema.sql",
        "app/bootstrap.py",
        "app/bot/main.py",
        "app/worker/main.py",
        "app/web/main.py",
        "docs/TRANSLATIONS.md",
        "docs/SOURCE-VERIFICATION.md",
        "data/source_snapshot.json",
    ]
    missing = [item for item in required if not (ROOT / item).is_file()]
    checks.append({"name": "required_files", "passed": not missing, "missing": missing})

    manifest_lines = [f"{sha256(path)}  {path.relative_to(ROOT).as_posix()}" for path in files()]
    (ROOT / "MANIFEST.sha256").write_text("\n".join(manifest_lines) + "\n", encoding="utf-8")

    total_bytes = sum(path.stat().st_size for path in files())
    passed = all(check.get("passed") for check in checks)
    report = {
        "project": "BibleMessengerBot",
        "version": (ROOT / "VERSION").read_text().strip(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "PASS" if passed else "FAIL",
        "files": len(files()),
        "bytes": total_bytes,
        "checks": checks,
        "not_executed": [
            "Docker image build (Docker is unavailable in the audit environment)",
            "PostgreSQL schema execution against a live server",
            "Live Bible corpus download and full first-run import",
            "Telegram Bot API send/receive operations",
            "Deployment on the target VPS",
        ],
    }
    (ROOT / "RELEASE-AUDIT.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )

    lines = [
        "# Release audit",
        "",
        f"- Project: `{report['project']}`",
        f"- Version: `{report['version']}`",
        f"- Status: **{report['status']}**",
        f"- Files: {report['files']}",
        f"- Bytes: {report['bytes']}",
        "",
        "## Checks",
        "",
    ]
    for check in checks:
        lines.append(f"- {'PASS' if check.get('passed') else 'FAIL'} — `{check['name']}`")
    lines.extend(["", "## Not executed in this environment", ""])
    lines.extend(f"- {item}" for item in report["not_executed"])
    (ROOT / "RELEASE-AUDIT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
