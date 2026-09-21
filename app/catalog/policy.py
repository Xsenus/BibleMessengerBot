"""Fail-closed edition licensing; incidental prose never grants redistribution rights."""
from __future__ import annotations
import re
from dataclasses import dataclass
from urllib.parse import urlparse
from app.catalog.models import TranslationMeta


@dataclass(frozen=True, slots=True)
class LicenseDecision:
    """An explicit decision together with the machine-readable reason."""
    allowed: bool
    normalized: str
    reason: str


def normalize_license(value: str) -> str:
    """Normalize license identifiers, not arbitrary permission guesses."""
    text = value.strip().lower().replace('creative commons', 'cc')
    text = text.replace('public-domain', 'public domain').replace('public_domain', 'public domain')
    return re.sub(r'\s+', ' ', text)


def _identifier(value: str) -> str | None:
    text = normalize_license(value)
    if text in {'public domain', 'public domain dedication', 'cc0', 'cc0 1.0', 'cc0-1.0', 'unlicense'}:
        return 'public-domain' if 'public' in text or text == 'unlicense' else 'cc0'
    match = re.fullmatch(r'(?:cc[ -])?(by(?:[- ](?:nc|nd|sa)){0,2})(?:[ -]?[1-4]\.0)?', text)
    if match:
        return match[1].replace(' ', '-')
    return None


def _url_identifier(value: str) -> str | None:
    parsed = urlparse(value.strip())
    if parsed.scheme not in {'http', 'https'} or parsed.hostname not in {'creativecommons.org', 'www.creativecommons.org'}:
        return None
    path = parsed.path.lower().strip('/')
    if re.fullmatch(r'publicdomain/(zero|mark)/1\.0(?:/.*)?', path):
        return 'cc0' if '/zero/' in f'/{path}/' else 'public-domain'
    match = re.fullmatch(r'licenses/(by(?:-(?:nc|nd|sa)){0,2})/[1-4]\.0(?:/.*)?', path)
    return match[1] if match else None


def decide_license(metadata: TranslationMeta, *, allow_restricted: bool = False,
                   allow_unknown: bool = False) -> LicenseDecision:
    """Accept only explicit PD/CC0/CC BY/CC BY-SA, unless deliberately overridden.

    Overrides never bypass upstream non-downloadable/non-redistributable flags.
    The default installer disables both overrides.
    """
    if not metadata.redistributable:
        return LicenseDecision(False, 'non-redistributable', 'upstream marks it non-redistributable')
    if not metadata.downloadable:
        return LicenseDecision(False, 'not-downloadable', 'upstream marks it non-downloadable')
    identifiers: set[str] = set()
    raw = []
    if metadata.license:
        raw.extend([metadata.license.license_type, metadata.license.license_url])
        for identifier in (_identifier(metadata.license.license_type), _url_identifier(metadata.license.license_url)):
            if identifier:
                identifiers.add(identifier)
    # A precise 'Public Domain' notice is accepted, not prose containing those words.
    notice_id = _identifier(metadata.copyright_notice)
    if notice_id:
        identifiers.add(notice_id)
    raw.append(metadata.copyright_notice)
    normalized = normalize_license(' '.join(raw))
    restricted = any('nc' in item.split('-') or 'nd' in item.split('-') for item in identifiers)
    restricted = restricted or any(x in normalized for x in ('all rights reserved', 'noncommercial', 'non-commercial', 'no derivatives'))
    if restricted:
        return LicenseDecision(allow_restricted, normalized, 'restricted; explicit operator permission required')
    equivalent = {'public-domain', 'cc0'}
    if len(identifiers) > 1 and not identifiers <= equivalent:
        return LicenseDecision(False, normalized, 'conflicting license declarations')
    if identifiers and identifiers <= {'public-domain', 'cc0', 'by', 'by-sa'}:
        return LicenseDecision(True, sorted(identifiers)[0], 'explicit approved license')
    return LicenseDecision(allow_unknown, normalized or 'unknown', 'license is not explicitly approved')
