"""Download the pinned public MAX CA over verified HTTPS; no global trust change."""
from __future__ import annotations

import argparse
import hashlib
import ssl
from pathlib import Path
from urllib.request import Request, urlopen

SOURCE='https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt'
# DER SHA-256 of Russian Trusted Root CA, valid 2022-03-01 through 2032-02-27.
# Certificate replacement requires an explicit source/fingerprint review.
FINGERPRINT='d26d2d0231b7c39f92cc738512ba54103519e4405d68b5bd703e9788ca8ecf31'


def prepare(output: Path):
    with urlopen(Request(SOURCE,headers={'User-Agent':'BibleMessengerBot/MAX-CA'}),
                 context=ssl.create_default_context(),timeout=30) as response:
        if response.geturl()!=SOURCE:
            raise ValueError('Unexpected certificate download redirect')
        data=response.read(32769)
    if len(data)>32768 or data.count(b'-----BEGIN CERTIFICATE-----')!=1:
        raise ValueError('Invalid public certificate download')
    der=ssl.PEM_cert_to_DER_cert(data.decode('ascii'))
    fingerprint=hashlib.sha256(der).hexdigest()
    if fingerprint!=FINGERPRINT:
        raise ValueError('CA fingerprint changed; review official certificate before use')
    if output.exists() and output.read_bytes()!=data:
        raise ValueError('Refusing to replace a different configured certificate')
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_bytes(data)
    output.chmod(0o644)  # Public CA only; this file contains no private key.
    print(f'Public MAX CA verified: sha256={fingerprint}; path={output}')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=Path('certs/max-ca.pem'))
    prepare(parser.parse_args().output)
