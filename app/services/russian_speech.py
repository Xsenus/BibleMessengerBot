"""Pinned Russian voices; CPU inference is isolated, with Piper retained separately."""
from __future__ import annotations

import hashlib
import json
import re
import sys
import wave
from pathlib import Path

import httpx

from app.services import neural_speech

MODEL = json.loads((Path(__file__).parents[1] / 'data/russian-voice.json').read_text(encoding='utf-8'))
SPEAKERS = MODEL['speakers']


def profile(speaker='aidar'):
    if speaker not in SPEAKERS.values():
        raise ValueError('unsupported_russian_voice')
    return f"neural-v2:silero:{MODEL['sha256'][:16]}:{speaker}"


def directory():
    return neural_speech.cache_root() / ('silero-' + MODEL['sha256'][:16])


def complete(*, verify=False):
    path = directory() / MODEL['name']
    try:
        if path.stat().st_size != MODEL['bytes']:
            return False
        if verify:
            with path.open('rb') as stream:
                return hashlib.file_digest(stream, 'sha256').hexdigest() == MODEL['sha256']
        return True
    except OSError:
        return False


async def prepare(*, client=None):
    """Never import the model package before its exact bytes/hash are verified."""
    if complete(verify=True):
        return
    root=directory()
    root.mkdir(parents=True,exist_ok=True)
    temporary=root/'model.pt.part'
    owned=client is None
    client=client or httpx.AsyncClient(timeout=180,follow_redirects=True)
    try:
        digest=hashlib.sha256()
        size=0
        async with client.stream('GET',MODEL['url']) as response:
            response.raise_for_status()
            with temporary.open('wb') as stream:
                async for block in response.aiter_bytes():
                    size+=len(block)
                    if size>MODEL['bytes']:
                        raise ValueError('russian_model_size_exceeded')
                    digest.update(block)
                    stream.write(block)
        if size!=MODEL['bytes'] or digest.hexdigest()!=MODEL['sha256']:
            raise ValueError('russian_model_checksum_mismatch')
        temporary.replace(root/MODEL['name'])
    finally:
        temporary.unlink(missing_ok=True)
        if owned:
            await client.aclose()


def spoken_input(text):
    """Spell source numerals out; the model otherwise silently drops digits."""
    from num2words import num2words
    return re.sub(r'\b[0-9]+\b',lambda match:num2words(int(match[0]),lang='ru'),text)


def render(text, target, speaker='aidar'):
    if speaker not in SPEAKERS.values() or not complete(verify=True):
        raise ValueError('russian_voice_not_installed')
    import torch
    torch.set_num_threads(1)
    torch.set_num_interop_threads(1)
    model=torch.package.PackageImporter(str(directory()/MODEL['name'])).load_pickle('tts_models','model')
    model.to(torch.device('cpu'))
    sample_rate=48000
    with wave.open(str(target),'wb') as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        for piece in neural_speech.segments(spoken_input(text)):
            with torch.inference_mode():
                audio=model.apply_tts(text=piece,speaker=speaker,sample_rate=sample_rate,put_accent=True,put_yo=True)
            if audio.numel()==0 or not torch.isfinite(audio).all():
                raise ValueError('invalid_russian_waveform')
            output.writeframes((audio.clamp(-1,1)*32767).to(torch.int16).cpu().numpy().astype('<i2').tobytes())
            output.writeframes(b'\0\0'*(sample_rate//7))


if __name__=='__main__':
    render(sys.stdin.read(),Path(sys.argv[1]),sys.argv[2])
