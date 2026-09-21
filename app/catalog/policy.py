"""Conservative Bible edition license policy."""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.catalog.models import TranslationMeta


@dataclass(frozen=True, slots=True)
class LicenseDecision:
    allowed: bool
    normalized: str
    reason: str


def normalize_license(value: str) -> str:
    text = value.strip().lower()
    text = text.replace("creative commons", "cc")
    text = text.replace("public-domain", "public domain")
    text = text.replace("public_domain", "public domain")
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"[^a-z0-9+.-]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def decide_license(
    metadata: TranslationMeta,
    *,
    allow_restricted: bool = False,
    allow_unknown: bool = False,
) -> LicenseDecision:
    if not metadata.redistributable:
        return LicenseDecision(False, "non-redistributable", "upstream marks it non-redistributable")
    if not metadata.downloadable:
        return LicenseDecision(False, "not-downloadable", "upstream marks it non-downloadable")

    raw = " ".join(
        value
        for value in (
            metadata.license.license_type if metadata.license else "",
            metadata.license.license_url if metadata.license else "",
            metadata.copyright_notice,
        )
        if value
    )
    normalized = normalize_license(raw)

    restricted_markers = (
        "by-nc",
        "by nc",
        "noncommercial",
        "non commercial",
        "by-nd",
        "by nd",
        "noderivatives",
        "no derivatives",
        "all rights reserved",
    )
    if any(marker in normalized for marker in restricted_markers):
        if allow_restricted:
            return LicenseDecision(True, normalized, "restricted license explicitly enabled")
        return LicenseDecision(False, normalized, "NC, ND, or otherwise restricted license")

    public_domain_markers = (
        "public domain",
        "cc0",
        "cc 0",
        "unlicense",
    )
    if any(marker in normalized for marker in public_domain_markers):
        return LicenseDecision(True, normalized, "public domain or CC0")

    # Accept attribution/share-alike licenses but not NC/ND variants.
    permissive_patterns = (
        r"(?:^|\s)(?:cc\s*)?by(?:[-\s]sa)?(?:\s|$)",
        r"creativecommons\.org licenses by(?:-sa)?",
    )
    if any(re.search(pattern, normalized) for pattern in permissive_patterns):
        return LicenseDecision(True, normalized, "CC BY or CC BY-SA")

    if allow_unknown:
        return LicenseDecision(True, normalized or "unknown", "unknown license explicitly enabled")
    return LicenseDecision(False, normalized or "unknown", "license is not explicitly approved")
