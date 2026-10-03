"""
Shared model cache — fetches live available models from each provider's API once per day.

All modules (autopilot, ai, backtest) import get_live_models() from here.
Cache TTL: 24 hours. Falls back to hardcoded list if API call fails.
Blacklists models that return 404 "model_not_supported" for 24 hours.
"""

import logging
import time
import httpx

logger = logging.getLogger(__name__)

# Module-level cache: {provider_id: {"models": [...], "fetched_at": timestamp}}
_cache: dict[str, dict] = {}
CACHE_TTL_SECONDS = 24 * 60 * 60  # 24 hours

# Blacklist: {provider_id: {model_id: blacklisted_at}}
_blacklist: dict[str, dict[str, float]] = {}
BLACKLIST_TTL = 24 * 60 * 60  # 24 hours

# Keywords that indicate a model is NOT for chat completions
_NON_CHAT_KEYWORDS = [
    "embed", "rerank", "ocr", "safety", "guard", "nemoguard",
    "translate", "tts", "asr", "speech", "recognition", "voicechat",
    "diffusion", "image", "vision", "parse", "detect", "search",
    "calibration", "generate", "relighting", "lipsync", "eyecontact",
    "optimization", "routing", "scoring", "denoise", "stemming",
    "diarize", "segment", "restore", "upscale", "super-resolution",
    "gliger", "flux", "stable-diffusion", "wan2",
]


def _is_chat_model(model_id: str) -> bool:
    """Check if a model ID looks like it supports chat completions."""
    lower = model_id.lower()
    for kw in _NON_CHAT_KEYWORDS:
        if kw in lower:
            return False
    return True


async def get_live_models(provider_id: str, api_key: str, base_url: str, needs_nvapi_prefix: bool = False) -> list[str]:
    """Get available models for a provider. Uses cache if fresh, otherwise fetches live.

    Returns list of model IDs. Falls back to empty list on failure.
    """
    now = time.time()

    # Check cache
    if provider_id in _cache:
        age = now - _cache[provider_id]["fetched_at"]
        if age < CACHE_TTL_SECONDS:
            models = _cache[provider_id]["models"]
            # Filter out blacklisted models
            bl = _blacklist.get(provider_id, {})
            return [m for m in models if m not in bl or now - bl[m] > BLACKLIST_TTL]

    # Fetch live models
    models = await _fetch_models_from_api(provider_id, api_key, base_url, needs_nvapi_prefix)

    if models:
        # Filter to chat-only models
        chat_models = [m for m in models if _is_chat_model(m)]
        if chat_models:
            _cache[provider_id] = {"models": chat_models, "fetched_at": now}
            logger.info(f"[models_cache] {provider_id}: cached {len(chat_models)} chat models (from {len(models)} total)")
        else:
            # No chat models found — keep all as fallback
            _cache[provider_id] = {"models": models, "fetched_at": now}
            logger.warning(f"[models_cache] {provider_id}: no chat models found, keeping all {len(models)} models")
    elif provider_id in _cache:
        models = _cache[provider_id]["models"]
        logger.warning(f"[models_cache] {provider_id}: API failed, using stale cache ({len(models)} models)")
    else:
        logger.warning(f"[models_cache] {provider_id}: API failed and no cache available")

    return models


async def _fetch_models_from_api(provider_id: str, api_key: str, base_url: str, needs_nvapi_prefix: bool) -> list[str]:
    """Call GET {base_url}/models to get available models."""
    if not api_key:
        return []

    try:
        headers = {"Authorization": f"Bearer {api_key}"}
        if needs_nvapi_prefix and not api_key.startswith("nvapi-"):
            headers["Authorization"] = f"Bearer nvapi-{api_key}"

        url = f"{base_url.rstrip('/')}/models"
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                models = [
                    item["id"] for item in data.get("data", [])
                    if item.get("id") and not item.get("deprecated", False)
                ]
                if models:
                    return sorted(models)
            else:
                logger.warning(f"[models_cache] {provider_id}: GET /models returned {resp.status_code}")
    except Exception as e:
        logger.warning(f"[models_cache] {provider_id}: fetch failed: {e}")

    return []


def blacklist_model(provider_id: str, model_id: str) -> None:
    """Blacklist a model that returned 404. Won't be used for 24 hours."""
    if provider_id not in _blacklist:
        _blacklist[provider_id] = {}
    _blacklist[provider_id][model_id] = time.time()
    logger.info(f"[models_cache] blacklisted {provider_id}/{model_id} for 24h")


def get_stale_models(provider_id: str) -> list[str]:
    """Synchronous fallback — returns cached models without fetching."""
    if provider_id in _cache:
        return _cache[provider_id]["models"]
    return []


def force_refresh_all(providers_config: dict, api_keys: dict) -> None:
    """Sync refresh for startup — call once at boot."""
    import asyncio

    async def _refresh():
        for pid, cfg in providers_config.items():
            key = api_keys.get(pid, "")
            models = await get_live_models(pid, key, cfg["base_url"], cfg.get("needs_nvapi_prefix", False))
            if not models and cfg.get("models"):
                _cache[pid] = {"models": cfg["models"], "fetched_at": time.time()}

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.create_task(_refresh())
        else:
            loop.run_until_complete(_refresh())
    except RuntimeError:
        asyncio.run(_refresh())
