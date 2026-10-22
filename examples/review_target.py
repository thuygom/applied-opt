"""Synthetic review fixture. No production inputs, services, or credentials.

This intentionally contains review findings. Use it to try the scanner and
the opt-in parallel patch; it is not a recommended application architecture.
"""

import asyncio
import time


async def fetch_context(subject_id: str):
    await asyncio.sleep(0.06)
    return {"subject": subject_id, "source": "synthetic"}


async def detect_typos(text: str):
    await asyncio.sleep(0.04)
    return []


async def generate_note(text: str, context: dict):
    await asyncio.sleep(0.03)
    return {"summary": text, "subject": context["subject"]}


async def sequential_pipeline(text: str, subject_id: str):
    context = await fetch_context(subject_id)
    typos = await detect_typos(text)
    note = await generate_note(text, context)
    return {"note": note, "typos": typos}


async def dependent_pipeline(subject_id: str):
    context = await fetch_context(subject_id)
    note = await generate_note("Synthetic note", context)
    return note


async def accidental_blocking():
    time.sleep(0.01)
    return "synthetic"


async def unbounded_batch(subject_ids):
    return await asyncio.gather(*(fetch_context(subject) for subject in subject_ids))


_cache = {}


async def analyze_note(text):
    cached = _cache.get(text)
    if cached is not None:
        return cached
    # This is an intentionally incomplete cache contract for the review demo.
    result = await generate_note(text, {"subject": "synthetic"})
    _cache[text] = result
    return result
