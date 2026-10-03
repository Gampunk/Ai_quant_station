#!/usr/bin/env python3
"""Test all AI provider API keys — quick health check."""
import asyncio
import sys
sys.path.insert(0, ".")

from app.core.config import settings
from app.core.providers import PROVIDERS, get_base_url

from openai import AsyncOpenAI

async def test_provider(name: str, cfg: dict):
    from app.core.providers import get_api_key
    key = get_api_key(name, settings)
    if not key:
        print(f"  {name:12s} | NO KEY SET")
        return
    
    base_url = get_base_url(name)
    model = cfg["models"][0] if cfg["models"] else "unknown"
    
    try:
        client = AsyncOpenAI(base_url=base_url, api_key=key)
        resp = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "Say 'OK' in one word."}],
            max_tokens=5,
            timeout=15,
        )
        reply = resp.choices[0].message.content.strip()
        print(f"  {name:12s} | OK | model={model} | reply={reply}")
    except Exception as e:
        err = str(e)[:80]
        print(f"  {name:12s} | FAIL | {err}")

async def main():
    print("Testing all AI providers:\n")
    for name, cfg in PROVIDERS.items():
        await test_provider(name, cfg)

asyncio.run(main())
