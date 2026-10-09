"""Voice profiles, bounded inference, integrity checks and real offline synthesis."""
import hashlib
import os
from unittest.mock import AsyncMock

import httpx
import pytest

from app.services import neural_speech as neural, speech,russian_speech


def test_profiles_are_distinct_and_neural_chunks_are_lossless():
    text = 'Ἐν ἀρχῇ ἦν ὁ λόγος. In principio erat Verbum. ' * 100
    parts = neural.segments(text)
    assert ''.join(parts) == text and all(0 < len(x) <= 350 for x in parts)
    for code in ('rus', 'lat', 'grc'):
        assert speech.voice_profile(code, speech.SpeechSettings()).startswith('neural-')
        assert speech.voice_profile(code, speech.SpeechSettings(provider='espeak')) == 'v1'
    assert len({neural.profile(code) for code in neural.LANGUAGES}) == 3


def test_latin_ligatures_and_j_are_not_silently_deleted():
    vocabulary = set('abcdefghijklmnopqrstuvwxyz ') - {'j', 'k', 'w'}
    assert neural.normalize_for_model('Jēsus, cælum et pœna.', 'lat', vocabulary) == 'iesus, caelum et poena.'
    with pytest.raises(ValueError, match='unsupported_neural_letter'):
        neural.normalize_for_model('Ж', 'lat', vocabulary)
    words = 'ἑξακόσιοι ἑξήκοντα ἕξ'
    assert neural.normalize_for_model('χξϛʹ', 'grc', set(words)) == words


@pytest.mark.asyncio
async def test_download_verifies_digest_and_repairs_corrupt_same_size_file(tmp_path, monkeypatch):
    data = b'checked-weights'
    model = dict(engine='fixture', files=[dict(name='model.onnx', url='https://example.test/model', bytes=len(data), sha256=hashlib.sha256(data).hexdigest())])
    monkeypatch.setattr(neural, 'MODELS', {'rus': model})
    monkeypatch.setenv('AUDIO_MODELS_PATH', str(tmp_path))
    calls = []
    def response(request):
        calls.append(request)
        return httpx.Response(200, content=data)
    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        await neural.prepare(client=client)
        assert neural.complete('rus')
        (neural.directory('rus') / 'model.onnx').write_bytes(b'x' * len(data))
        await neural.prepare(client=client)
        assert (neural.directory('rus') / 'model.onnx').read_bytes() == data
        await neural.prepare(client=client)
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_failed_download_never_marks_model_ready(tmp_path, monkeypatch):
    model = dict(engine='fixture', files=[dict(name='model.onnx', url='https://example.test/model', bytes=3, sha256='0' * 64)])
    monkeypatch.setattr(neural, 'MODELS', {'rus': model})
    monkeypatch.setenv('AUDIO_MODELS_PATH', str(tmp_path))
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b'bad'))) as client:
        with pytest.raises(ValueError, match='checksum'):
            await neural.prepare(client=client)
    assert not neural.complete('rus')
    assert not list(tmp_path.rglob('*.part'))


@pytest.mark.asyncio
@pytest.mark.parametrize('language,engine', [('rus', 'silero'), ('lat', 'mms'), ('grc', 'mms')])
async def test_quality_languages_never_fall_back_to_robotic_voice(language, engine, monkeypatch):
    run = AsyncMock(return_value=b'')
    monkeypatch.setattr(speech, 'run_process', run)
    monkeypatch.setattr(speech, 'validate_audio', AsyncMock(return_value=(b'ID3' + b'x' * 700, 5)))
    result = await speech.synthesize('text', language, speech.SpeechSettings())
    assert result[2] == engine and result[3] == speech.voice_profile(language,speech.SpeechSettings())
    assert run.await_args_list[0].args[2] == ('app.services.russian_speech' if language=='rus' else 'app.services.neural_speech')
    assert not any(call.args[0] == 'espeak-ng' for call in run.await_args_list)
    run.side_effect = RuntimeError('neural worker unavailable')
    with pytest.raises(RuntimeError):
        await speech.synthesize('text', language, speech.SpeechSettings())


@pytest.mark.asyncio
@pytest.mark.skipif(os.getenv('RUN_NEURAL_TESTS') != '1', reason='Pinned local neural models required')
@pytest.mark.parametrize('language,text', [
    ('rus', 'В начале сотворил Бог небо и землю.'),
    ('lat', 'In principio erat Verbum, et Verbum erat apud Deum.'),
    ('grc', 'Ἐν ἀρχῇ ἦν ὁ λόγος, καὶ ὁ λόγος ἦν πρὸς τὸν θεόν.'),
])
async def test_real_neural_voice_produces_valid_audio(language, text):
    data, duration, provider, voice = await speech.synthesize(text, language, speech.SpeechSettings())
    assert len(data) > 512 and duration >= 2
    assert provider == ('silero' if language=='rus' else neural.MODELS[language]['engine']) and voice == speech.voice_profile(language,speech.SpeechSettings())


@pytest.mark.skipif(os.getenv('RUN_NEURAL_TESTS') != '1', reason='Pinned local neural models required')
@pytest.mark.parametrize('speaker,engine',[('kseniya','silero'),('aidar','piper')])
async def test_real_selected_voice_and_preserved_reserve(speaker,engine):
    settings=speech.SpeechSettings(russian_voice=speaker,russian_engine=engine)
    data,duration,provider,voice=await speech.synthesize('Ему было двести тридцать лет. Любовь долготерпит, милосердствует.', 'rus',settings)
    assert len(data)>512 and duration>=2 and provider==engine
    assert voice==speech.voice_profile('rus',settings)
