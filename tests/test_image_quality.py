import asyncio
import io
import shutil
from unittest.mock import AsyncMock

import httpx
import pytest
from PIL import Image, ImageDraw, ImageFont

from app.services import image_quality, visual_scene


def page():
    picture = Image.new("RGB", (800, 600), "white")
    draw = ImageDraw.Draw(picture)
    font = ImageFont.load_default(size=28)
    for i in range(8):
        draw.text(
            (25, 25 + i * 60),
            "Create one biblical illustration TARGET VERSE",
            font=font,
            fill="black",
        )
    output = io.BytesIO()
    picture.save(output, format="PNG")
    return output.getvalue()


def test_low_confidence_texture_glyphs_do_not_reject_a_scene():
    header = "level\tblock_num\tpar_num\tline_num\tconf\ttext\n"
    texture = header + "".join(f"5\t1\t1\t{i}\t35\txx xx xx\n" for i in range(100))
    assert image_quality.classify(texture)["verdict"] == "approved"
    writing = header + "".join(f"5\t1\t1\t{i}\t90\tBiblical\n" for i in range(8))
    assert image_quality.classify(writing)["verdict"] == "rejected"


@pytest.mark.asyncio
@pytest.mark.skipif(shutil.which("tesseract") is None, reason="Local OCR binary is required")
async def test_real_ocr_rejects_instruction_pages_and_accepts_blank_images():
    assert (await image_quality.assess(page()))["verdict"] == "rejected"
    out = io.BytesIO()
    Image.new("RGB", (800, 600), "gray").save(out, format="PNG")
    assert (await image_quality.assess(out.getvalue()))["verdict"] == "approved"


@pytest.mark.asyncio
async def test_missing_ocr_fails_closed(monkeypatch):
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", AsyncMock(side_effect=FileNotFoundError())
    )
    assert (await image_quality.assess(b"fixture"))["verdict"] == "unavailable"


@pytest.mark.asyncio
async def test_stability_planner_sends_only_bounded_frozen_source():
    scene = "An elderly shepherd gently helps a tired traveller along a rocky path, surrounded by olive trees in warm morning sunlight."

    def handle(request):
        import json

        payload = json.loads(request.content)
        assert "UNTRUSTED PROMPT" not in payload["messages"][1]["content"]
        assert "Passage about mercy" in payload["messages"][1]["content"]
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps({"scene": scene})}}]}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await visual_scene.describe(
            "UNTRUSTED PROMPT <verse>Passage about mercy</verse>", client=client
        )
    assert result.startswith(scene)


@pytest.mark.asyncio
async def test_stability_planner_rejects_page_descriptions():
    import json

    scene = "A printed page with ornate lettering and a large book, with many lines of text in a decorative frame."
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, json={"choices": [{"message": {"content": json.dumps({"scene": scene})}}]}
            )
        )
    ) as client:
        with pytest.raises(ValueError):
            await visual_scene.describe("<verse>mercy</verse>", client=client)
