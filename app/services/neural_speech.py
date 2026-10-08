"""Pinned local neural voices. No remote model code, paid API or robotic fallback."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import unicodedata
from contextlib import asynccontextmanager
from pathlib import Path

import httpx

MANIFEST_PATH = Path(__file__).parents[1] / 'data' / 'speech-models.json'
MODELS = json.loads(MANIFEST_PATH.read_text(encoding='utf-8'))
LANGUAGES = tuple(MODELS)


def cache_root():
    return Path(os.getenv('AUDIO_MODELS_PATH', '/app/cache/tts'))


def profile(language):
    model = MODELS[language]
    digest = hashlib.sha256(json.dumps(model, sort_keys=True).encode()).hexdigest()[:16]
    return f'neural-v1:{model["engine"]}:{digest}'


def directory(language):
    return cache_root() / profile(language).replace(':', '-')


def complete(language):
    root = directory(language)
    try:
        return (root / 'READY').read_text() == profile(language) and all(
            (root / item['name']).stat().st_size == item['bytes'] for item in MODELS[language]['files'])
    except OSError:
        return False


async def prepare(*, client=None):
    """Download only declared files, atomically, verifying exact size and SHA-256."""
    async with httpx.AsyncClient(timeout=180, follow_redirects=True) if client is None else _client(client) as api:
        for language, model in MODELS.items():
            root = directory(language)
            root.mkdir(parents=True, exist_ok=True)
            (root / 'READY').unlink(missing_ok=True)
            for item in model['files']:
                target = root / item['name']
                if target.is_file() and target.stat().st_size == item['bytes']:
                    with target.open('rb') as stream:
                        if hashlib.file_digest(stream, 'sha256').hexdigest() == item['sha256']:
                            continue
                temporary = target.with_suffix(target.suffix + '.part')
                try:
                    digest = hashlib.sha256(); size = 0
                    async with api.stream('GET', item['url']) as response:
                        response.raise_for_status()
                        with temporary.open('wb') as stream:
                            async for data in response.aiter_bytes():
                                size += len(data)
                                if size > item['bytes']:
                                    raise ValueError('model_size_exceeded')
                                digest.update(data); stream.write(data)
                    if size != item['bytes'] or digest.hexdigest() != item['sha256']:
                        raise ValueError('model_checksum_mismatch')
                    temporary.replace(target)
                finally:
                    temporary.unlink(missing_ok=True)
            (root / 'READY').write_text(profile(language))


@asynccontextmanager
async def _client(client):
    yield client


def segments(text, maximum=350):
    """Bound inference RAM while preserving every character and word in order."""
    from app.services.speech import portions
    result = []; start = 0
    for boundary in re.finditer(r'(?<=[.!?;:·])\s+', text):
        result.extend(portions(text[start:boundary.end()], maximum))
        start = boundary.end()
    result.extend(portions(text[start:], maximum))
    return result


def normalize_for_model(text, language, vocabulary):
    """Preserve letters the checkpoint tokenizer would otherwise silently delete."""
    text = unicodedata.normalize('NFC', text).lower()
    if language == 'lat':
        text = text.translate(str.maketrans({'æ': 'ae', 'œ': 'oe', 'j': 'i', 'k': 'c', 'w': 'v'}))
    elif language == 'grc':
        # Alphabetic numeral in Rev 13:18; speak its value, not dropped stigma.
        text = re.sub(r'(?<!\w)χξϛ[ʹʹ′\']?(?!\w)', 'ἑξακόσιοι ἑξήκοντα ἕξ', text)
    result = []
    for character in text:
        if character in vocabulary or not character.isalpha():
            result.append(character)
            continue
        base = ''.join(c for c in unicodedata.normalize('NFD', character) if not unicodedata.combining(c))
        if all(c in vocabulary for c in base):
            result.append(base)
        else:
            raise ValueError('unsupported_neural_letter')
    return ''.join(result)


def render(language, text, target):
    """Runs in an isolated child process: heavyweight memory is released on exit."""
    import wave
    if language not in MODELS or not complete(language):
        raise ValueError('neural_voice_not_installed')
    root = directory(language)
    if language == 'rus':
        from piper import PiperVoice, SynthesisConfig
        voice = PiperVoice.load(str(root / 'ru_RU-denis-medium.onnx'))
        with wave.open(str(target), 'wb') as output:
            configured = False
            for piece in segments(text):
                for chunk in voice.synthesize(piece, syn_config=SynthesisConfig(length_scale=1.05)):
                    if not configured:
                        output.setnchannels(chunk.sample_channels)
                        output.setsampwidth(chunk.sample_width)
                        output.setframerate(chunk.sample_rate)
                        configured = True
                    output.writeframes(chunk.audio_int16_bytes)
                if configured:
                    output.writeframes(b'\0\0' * (output.getframerate() // 8))
        return
    import numpy as np
    import torch
    from transformers import VitsModel, VitsTokenizer
    torch.set_num_threads(1)
    torch.set_num_interop_threads(1)
    torch.manual_seed(0)
    model = VitsModel.from_pretrained(str(root), local_files_only=True, use_safetensors=True, trust_remote_code=False).eval()
    tokenizer = VitsTokenizer.from_pretrained(str(root), local_files_only=True, trust_remote_code=False)
    sample_rate = model.config.sampling_rate
    with wave.open(str(target), 'wb') as output:
        output.setnchannels(1); output.setsampwidth(2); output.setframerate(sample_rate)
        for piece in segments(text):
            inputs = tokenizer(normalize_for_model(piece, language, tokenizer.get_vocab()), return_tensors='pt')
            with torch.inference_mode():
                waveform = model(**inputs).waveform[0].cpu().numpy()
            if waveform.size == 0 or not np.isfinite(waveform).all():
                raise ValueError('invalid_neural_waveform')
            output.writeframes((np.clip(waveform, -1, 1) * 32767).astype('<i2').tobytes())
            output.writeframes(b'\0\0' * (sample_rate // 8))


if __name__ == '__main__':
    os.environ['HF_HUB_OFFLINE'] = '1'
    os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'
    os.environ['TOKENIZERS_PARALLELISM'] = 'false'
    os.environ['OMP_NUM_THREADS'] = '1'
    render(sys.argv[1], sys.stdin.read(), Path(sys.argv[2]))
