"""Edition-level evidence. A repository's software license is never the Bible's license."""
from __future__ import annotations
import re
from dataclasses import replace
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit

from app.catalog.models import LicenseInfo
from app.catalog.policy import _identifier, _url_identifier
from app.catalog.multisource.types import Candidate, safe_id
from app.catalog.multisource.transport import Downloader, digest_file


class LicenseHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.chunks=[];self.links=[];self.hidden=0
    def handle_starttag(self,tag,attrs):
        if tag in {'script','style'}:self.hidden+=1
        if tag=='a':
            href=dict(attrs).get('href')
            if href:self.links.append(href)
    def handle_endtag(self,tag):
        if tag in {'script','style'} and self.hidden:self.hidden-=1
    def handle_data(self,data):
        if not self.hidden and data.strip():self.chunks.append(data.strip())


def parse_license_html(text: str) -> tuple[str,str,str]:
    """Accept exact standalone PD labels or an official CC URL, not generic prose."""
    parser=LicenseHTML();parser.feed(text)
    urls={url:_url_identifier(url) for url in parser.links if _url_identifier(url)}
    ids=set(urls.values())
    labels={_identifier(chunk) for chunk in parser.chunks}
    ids.update(x for x in labels if x)
    full_notice=' '.join(parser.chunks)
    # The full page remains in the hashed raw cache. Only rights-related text is
    # presented with verses; site navigation must not become a 16KB attribution.
    rights=[]
    for i,chunk in enumerate(parser.chunks):
        if _identifier(chunk) or re.search(r'copyright|©|translation by|translated by',chunk,re.I):
            rights.append(chunk)
            if re.search(r'(?:copyright|©).*\d{4}\s*$',chunk,re.I) and i+1<len(parser.chunks):
                rights.append(parser.chunks[i+1])
    notice=' · '.join(dict.fromkeys(rights))
    if len(notice)>4000:
        return 'unknown','','Attribution exceeds safe metadata budget; manual review required'
    lower=full_notice.lower()
    if any(x in lower for x in ('all rights reserved','non-commercial','noncommercial','no derivatives')):
        return 'restricted','','Restricted terms / all rights reserved; manual review required'
    if len(ids)>1 and not ids <= {'public-domain','cc0'}:
        return 'conflicting','','Conflicting license declarations; manual review required'
    if not ids:return 'unknown','',notice[:16000]
    identifier=sorted(ids)[0]
    url=next((url for url,value in urls.items() if value==identifier),'')
    return ('Public Domain' if identifier=='public-domain' else identifier),url,notice[:16000]


def ebible_id(row: dict) -> str | None:
    for key in ('licenseUrl','website'):
        parsed=urlsplit(str(row.get(key,'')))
        if parsed.hostname in {'ebible.org','www.ebible.org'}:
            identifier=parse_qs(parsed.query).get('id',[''])[0]
            if identifier:return safe_id(identifier)
    return None


async def resolve_hello_license(candidate: Candidate,downloader: Downloader,refresh: bool) -> Candidate:
    """Independent eBible page lookup, so HelloAO does not require GitHub availability."""
    identifier=ebible_id(candidate.raw)
    if identifier:
        url=f'https://ebible.org/find/show.php?id={identifier}'
        path=await downloader.fetch(url,refresh=refresh,max_bytes=2*1024**2)
        kind,license_url,notice=parse_license_html(path.read_text(encoding='utf-8-sig'))
        info=LicenseInfo(candidate.metadata.translation_id,kind,license_url=license_url)
        return replace(candidate,metadata=replace(candidate.metadata,license=info,
            copyright_notice=notice), evidence={**candidate.evidence,'license_page':url,'license_sha256':digest_file(path)})
    # Exact edition-specific primary-source declaration. Not applied to other translations.
    if candidate.metadata.translation_id=='BSB' and candidate.raw.get('language')=='eng' and urlsplit(str(candidate.raw.get('licenseUrl',''))).hostname in {'berean.bible','www.berean.bible'}:
        url='https://berean.bible/licensing.htm'
        path=await downloader.fetch(url,refresh=refresh,max_bytes=2*1024**2)
        p=LicenseHTML();p.feed(path.read_text(encoding='utf-8-sig'))
        statement=' '.join(p.chunks)
        if re.search(r'The Berean Bible and Majority Bible texts are officially placed into the public domain as of April 30, 2023',statement,re.I):
            return replace(candidate,metadata=replace(candidate.metadata,license=LicenseInfo('BSB','Public Domain'),copyright_notice='Public Domain'),
                evidence={**candidate.evidence,'license_page':url,'license_sha256':digest_file(path),'scope':'BSB text declaration; website footer is not the text license'})
    return candidate  # remains unknown and is blocked by policy
