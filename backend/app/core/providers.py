"""
Central AI Provider Registry
All provider configuration lives here. Imported by ai.py, autopilot.py,
historical_lab.py, backtest.py — single source of truth.

Supports comma-separated API keys per provider for automatic fallback.
Set NVIDIA_API_KEY=key1,key2,key3 in .env — each key is tried in order.
"""
from typing import Dict, Any, List, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select


# Each provider entry:
#   name: display name for frontend
#   env_key: settings attribute name for the API key
#   base_url: OpenAI-compatible API endpoint
#   models: default model list (overridden by live fetch)
PROVIDERS: Dict[str, Dict[str, Any]] = {
    "nvidia": {
        "name": "NVIDIA NIM",
        "env_key": "NVIDIA_API_KEY",
        "base_url": "https://integrate.api.nvidia.com/v1",
        "needs_nvapi_prefix": True,
        "models": [
            "nvidia/llama-3.3-nemotron-super-49b-v1",
            "nvidia/llama-3.1-nemotron-nano-8b-v1",
            "nvidia/llama-3.1-8b-instruct",
            "nvidia/deepseek-r1",
            "nvidia/mistral-nemotron",
            "nvidia/nemotron-3.5-lightning-30b-a3b",
            "deepseek-ai/deepseek-v4-flash-0731",
            "mistralai/mistral-large-2-instruct",
        ],
    },
    "groq": {
        "name": "Groq",
        "env_key": "GROQ_API_KEY",
        "base_url": "https://api.groq.com/openai/v1",
        "needs_nvapi_prefix": False,
        "models": [
            "llama-3.1-8b-instant",
            "mixtral-8x7b-32768",
            "gemma2-9b-it",
        ],
    },
    "openrouter": {
        "name": "OpenRouter",
        "env_key": "OPEN_ROUTER_API_KEY",
        "base_url": "https://openrouter.ai/api/v1",
        "needs_nvapi_prefix": False,
        "models": [
            "openai/gpt-4o-mini",
            "openai/gpt-4o",
            "meta-llama/llama-3.1-70b-instruct",
            "google/gemini-2.0-flash-001",
        ],
    },
    "gemini": {
        "name": "Google Gemini",
        "env_key": "GEMINI_API_KEY",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "needs_nvapi_prefix": False,
        "models": [
            "gemini-3.6-flash",
            "gemini-3.6-pro",
            "gemini-2.5-flash",
            "gemini-2.5-pro",
            "gemini-1.5-flash",
            "gemini-1.5-pro",
        ],
    },
    "github": {
        "name": "GitHub Models",
        "env_key": "GITHUB_API_KEY",
        "base_url": "https://models.inference.ai.azure.com",
        "needs_nvapi_prefix": False,
        "models": ["gpt-4o", "gpt-4o-mini", "meta-llama-3.1-70b-instruct"],
    },
    "cerebras": {
        "name": "Cerebras",
        "env_key": "CEREBRAS_API_KEY",
        "base_url": "https://api.cerebras.ai/v1",
        "needs_nvapi_prefix": False,
        "models": ["gpt-oss-120b", "llama3.1-8b"],
    },
    "mistral": {
        "name": "Mistral AI",
        "env_key": "MISTRAL_API_KEY",
        "base_url": "https://api.mistral.ai/v1",
        "needs_nvapi_prefix": False,
        "models": ["mistral-large-latest", "mistral-small-latest", "open-mistral-nemo"],
    },
    "anthropic": {
        "name": "Anthropic Claude",
        "env_key": "ANTHROPIC_API_KEY",
        "base_url": "https://api.anthropic.com/v1",
        "needs_nvapi_prefix": False,
        "models": [
            "claude-sonnet-4-20250514",
            "claude-3-5-sonnet-20241022",
            "claude-3-5-haiku-20241022",
            "claude-opus-4-20250514",
        ],
    },
    "tokenlb": {
        "name": "TokenLB Gateway",
        "env_key": "TOKENLB_API_KEY",
        "base_url": "https://tokenlb.net/v1",
        "needs_nvapi_prefix": False,
        "models": [
            "gpt-5.4-pro",
            "gpt-5.4",
            "gpt-5.4-mini",
            "claude-sonnet-4",
            "claude-3-5-sonnet",
            "gemini-2.5-pro",
        ],
    },
    "zenmux": {
        "name": "ZenMux",
        "env_key": "ZENMUX_API_KEY",
        "base_url": "https://zenmux.ai/api/v1",
        "needs_nvapi_prefix": False,
        "models": [
            "z-ai/glm-5.2",
        ],
    },
    "deepseek": {
        "name": "DeepSeek",
        "env_key": "DEEPSEEK_API_KEY",
        "base_url": "https://api.deepseek.com/v1",
        "needs_nvapi_prefix": False,
        "models": [
            "deepseek-chat",
            "deepseek-reasoner",
        ],
    },
    "qwen": {
        "name": "Alibaba Qwen",
        "env_key": "QWEN_API_KEY",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "needs_nvapi_prefix": False,
        "models": [
            "qwen-plus",
            "qwen-turbo",
            "qwen-max",
            "qwen-long",
        ],
    },
    "grok": {
        "name": "xAI Grok",
        "env_key": "XAI_API_KEY",
        "base_url": "https://api.x.ai/v1",
        "needs_nvapi_prefix": False,
        "models": [
            "grok-3-beta",
            "grok-3-mini-beta",
            "grok-2-1212",
        ],
    },
}


# Cost per 1M tokens (input, output) for each provider.
# Model-specific overrides override the provider default.
# Prices are in USD per 1M tokens. Set to 0 for free providers.
# Update these as provider pricing changes.
PRICING: Dict[str, Dict[str, Any]] = {
    "nvidia": {
        "default": (0.50, 1.50),
        "models": {
            "nvidia/llama-3.3-nemotron-super-49b-v1": (0.80, 2.50),
            "nvidia/llama-3.1-nemotron-nano-8b-v1": (0.20, 0.60),
            "nvidia/llama-3.1-8b-instruct": (0.20, 0.60),
            "nvidia/deepseek-r1": (0.55, 2.19),
            "nvidia/mistral-nemotron": (0.50, 1.50),
            "nvidia/nemotron-3.5-lightning-30b-a3b": (0.30, 0.90),
            "deepseek-ai/deepseek-v4-flash-0731": (0.27, 1.10),
            "mistralai/mistral-large-2-instruct": (2.00, 6.00),
        },
    },
    "groq": {
        "default": (0.50, 0.70),
        "models": {
            "llama-3.3-70b-versatile": (0.59, 0.79),
            "llama-3.1-8b-instant": (0.05, 0.08),
            "mixtral-8x7b-32768": (0.24, 0.24),
        },
    },
    "openrouter": {
        "default": (2.00, 8.00),
        "models": {},
    },
    "gemini": {
        "default": (0.50, 2.00),
        "models": {
            "gemini-2.5-flash": (0.10, 0.40),
            "gemini-2.5-pro": (1.25, 5.00),
            "gemini-1.5-flash": (0.08, 0.30),
            "gemini-1.5-pro": (1.00, 4.00),
        },
    },
    "github": {
        "default": (0.0, 0.0),
        "models": {},
    },
    "cerebras": {
        "default": (0.60, 0.60),
        "models": {},
    },
    "mistral": {
        "default": (2.00, 6.00),
        "models": {},
    },
    "anthropic": {
        "default": (3.00, 15.00),
        "models": {
            "claude-sonnet-4-20250514": (3.00, 15.00),
            "claude-3-5-sonnet-20241022": (3.00, 15.00),
            "claude-3-5-haiku-20241022": (0.80, 4.00),
            "claude-opus-4-20250514": (15.00, 75.00),
        },
    },
    "tokenlb": {
        "default": (2.00, 8.00),
        "models": {},
    },
    "zenmux": {
        "default": (0.0, 0.0),
        "models": {},
    },
    "deepseek": {
        "default": (0.14, 0.28),
        "models": {
            "deepseek-chat": (0.14, 0.28),
            "deepseek-reasoner": (0.55, 2.19),
        },
    },
    "qwen": {
        "default": (0.30, 0.60),
        "models": {
            "qwen-plus": (0.30, 0.60),
            "qwen-turbo": (0.05, 0.20),
            "qwen-max": (1.60, 6.40),
            "qwen-long": (0.05, 0.20),
        },
    },
    "grok": {
        "default": (3.00, 15.00),
        "models": {
            "grok-3-beta": (3.00, 15.00),
            "grok-3-mini-beta": (0.30, 0.50),
            "grok-2-1212": (2.00, 10.00),
        },
    },
}


def get_pricing(provider_id: str, model_name: str) -> tuple:
    """Return (input_cost_per_1M, output_cost_per_1M) for a provider/model pair."""
    cfg = PRICING.get(provider_id)
    if not cfg:
        return (0.0, 0.0)
    model_prices = cfg.get("models", {})
    if model_name in model_prices:
        return model_prices[model_name]
    return cfg.get("default", (0.0, 0.0))


def estimate_cost(prompt_tokens: int, completion_tokens: int, provider_id: str, model_name: str) -> float:
    """Estimate cost in USD for a given API call."""
    input_rate, output_rate = get_pricing(provider_id, model_name)
    return (prompt_tokens * input_rate + completion_tokens * output_rate) / 1_000_000


def get_api_key(provider_id: str, settings_obj) -> str:
    """Get the FIRST API key for a provider from settings. Backward compatible."""
    keys = get_all_api_keys(provider_id, settings_obj)
    return keys[0] if keys else ""


def get_all_api_keys(provider_id: str, settings_obj) -> List[str]:
    """Get ALL API keys for a provider (supports comma-separated).

    Returns a list of keys in order. Each key is tried in sequence by the
    retry logic, giving automatic failover when a key expires or rate-limits.
    """
    cfg = PROVIDERS.get(provider_id)
    if not cfg:
        return []
    raw = getattr(settings_obj, cfg["env_key"], "")
    if not raw:
        return []
    # Split on comma, strip whitespace, filter empty strings
    keys = [k.strip() for k in raw.split(",") if k.strip()]
    # Apply nvapi prefix if needed
    if cfg.get("needs_nvapi_prefix"):
        keys = [k if k.startswith("nvapi-") else f"nvapi-{k}" for k in keys]
    return keys


def get_base_url(provider_id: str) -> str:
    """Get the base URL for a provider."""
    cfg = PROVIDERS.get(provider_id)
    return cfg["base_url"] if cfg else ""


def validate_provider(provider_id: str) -> bool:
    """Check if a provider exists in the registry."""
    return provider_id in PROVIDERS


def get_provider_names() -> list:
    """Return list of provider IDs for API responses."""
    return list(PROVIDERS.keys())


async def resolve_api_key(
    provider: str,
    settings_obj,
    user_id: Optional[int] = None,
    db_session_factory=None,
) -> str:
    """Check user's saved key (encrypted in DB) first, fall back to server .env key.
    Returns the FIRST key only — backward compatible.
    """
    keys = await resolve_all_api_keys(provider, settings_obj, user_id, db_session_factory)
    return keys[0] if keys else ""


async def resolve_all_api_keys(
    provider: str,
    settings_obj,
    user_id: Optional[int] = None,
    db_session_factory=None,
) -> List[str]:
    """Check user's saved keys (encrypted in DB) first, fall back to server .env keys.

    User DB keys take priority. If the user has saved keys for this provider,
    those are returned (split by comma if multiple). Otherwise, returns all
    comma-separated keys from the server .env.

    Returns a list of keys in priority order for automatic fallback.
    """
    # 1. Try user's saved keys from DB
    if user_id and db_session_factory:
        from .encryption import decrypt_api_key
        from ..models.user import UserApiKey
        async with db_session_factory() as db:
            result = await db.execute(
                select(UserApiKey).where(
                    UserApiKey.user_id == user_id, UserApiKey.provider == provider
                )
            )
            row = result.scalar_one_or_none()
            if row:
                from cryptography.fernet import InvalidToken
                try:
                    decrypted = decrypt_api_key(row.encrypted_key, settings_obj.SECRET_KEY)
                except InvalidToken:
                    # Encrypted under a different SECRET_KEY, for example after rotating it.
                    # Fall back to the server keys rather than failing the request.
                    import logging
                    logging.getLogger(__name__).warning(
                        "Saved %s key for user %s cannot be decrypted with the current "
                        "SECRET_KEY. Using the server keys instead; the user should save it again.",
                        provider, user_id,
                    )
                    decrypted = ""
                if decrypted:
                    # User may have stored comma-separated keys too
                    user_keys = [k.strip() for k in decrypted.split(",") if k.strip()]
                    if user_keys:
                        return user_keys

    # 2. Fall back to server .env keys
    return get_all_api_keys(provider, settings_obj)
