"""Bounded local OCR gate: manuscript/poster output must never be published."""

from __future__ import annotations

import asyncio
import csv
import io
import json


def classify(tsv):
    words = []
    for row in csv.DictReader(io.StringIO(tsv), delimiter="\t"):
        try:
            text = row["text"].strip()
            confidence = float(row["conf"])
            letters = sum(character.isalpha() for character in text)
            if confidence >= 20 and letters >= 2:
                words.append(
                    (letters, confidence, (row["block_num"], row["par_num"], row["line_num"]))
                )
        except (KeyError, TypeError, ValueError):
            raise ValueError("Invalid OCR result") from None
    letters = sum(word[0] for word in words)
    lines = len({word[2] for word in words})
    confident = [word for word in words if word[1] >= 65 and word[0] >= 3]
    strong = sum(word[0] for word in confident)
    aligned = {}
    for word in confident:
        aligned.setdefault(word[2], []).append(word)
    # Sparse OCR hallucinates low-confidence glyphs in leaves/stone textures.
    # Require substantial confident writing or one coherent caption line.
    rejected = (strong >= 45 and len(confident) >= 5) or any(
        len(line) >= 4 and sum(word[0] for word in line) >= 20 for line in aligned.values()
    )
    return dict(
        verdict="rejected" if rejected else "approved",
        reason="visible_text" if rejected else "no_significant_text",
        metrics=dict(
            words=len(words),
            letters=letters,
            lines=lines,
            strong_letters=strong,
            strong_words=len(confident),
        ),
    )


async def assess(data, *, timeout=30):
    """OCR consumes image bytes through stdin; no external text/image API."""
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            "tesseract",
            "stdin",
            "stdout",
            "-l",
            "eng+rus",
            "--psm",
            "11",
            "tsv",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        output, _ = await asyncio.wait_for(process.communicate(data), timeout)
        if process.returncode or not output.startswith(b"level\t") or len(output) > 2_000_000:
            raise ValueError("OCR unavailable or malformed")
        return classify(output.decode("utf-8"))
    except (OSError, ValueError, UnicodeError, TimeoutError):
        if process and process.returncode is None:
            process.kill()
            await process.wait()
        return dict(verdict="unavailable", reason="ocr_unavailable", metrics={})
    except BaseException:
        if process and process.returncode is None:
            process.kill()
            await process.wait()
        raise


async def record(connection, image_id, attempt_id, result, *, data=None):
    await connection.execute(
        """INSERT INTO image_quality_reviews(image_id,attempt_id,verdict,reason,metrics,rejected_data)
        VALUES($1,$2,$3,$4,$5::jsonb,$6)""",
        image_id,
        attempt_id,
        result["verdict"],
        result["reason"],
        json.dumps(result["metrics"]),
        data if result["verdict"] != "approved" else None,
    )


async def gate(connection, image_id, attempt_id, data):
    result = await assess(data)
    await record(connection, image_id, attempt_id, result, data=data)
    return result["verdict"]
