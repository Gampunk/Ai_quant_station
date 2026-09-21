"""
Central AI Provider Registry
All provider configuration lives here. Imported by ai.py, autopilot.py,
historical_lab.py, backtest.py — single source of truth.
"""
from typing import Dict, Any, Optional
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
            "qwen/qwen3.5-122b-a10b",
            "qwen/qwen2.5-coder-32b-instruct",
            "deepseek-ai/deepseek-v3.1",
            "deepseek-ai/deepseek-r1-distill-qwen-32b",
            "nvidia/llama-3.1-405b-instruct",
        ],
    },
    "groq": {
        "name": "Groq",
        "env_key": "GROQ_API_KEY",
        "base_url": "https://api.groq.com/openai/v1",
        "needs_nvapi_prefix": False,
        "models": [
            "llama-3.3-70b-versatile",
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
}


# Cost per 1M tokens (input, output) for each provider.
# Model-specific overrides override the provider default.
# Prices are in USD per 1M tokens. Set to 0 for free providers.
# Update these as provider pricing changes.
PRICING: Dict[str, Dict[str, Any]] = {
    "nvidia": {
        "default": (1.50, 5.00),
        "models": {
            "deepseek-ai/deepseek-v3.1": (0.50, 2.00),
            "deepseek-ai/deepseek-r1-distill-qwen-32b": (0.50, 2.00),
            "nvidia/llama-3.1-405b-instruct": (3.00, 10.00),
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
    """Get the API key for a provider from settings."""
    cfg = PROVIDERS.get(provider_id)
    if not cfg:
        return ""
    val = getattr(settings_obj, cfg["env_key"], "")
    if cfg.get("needs_nvapi_prefix") and val and not val.startswith("nvapi-"):
        return f"nvapi-{val}"
    return val


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
    """Check user's saved key (encrypted in DB) first, fall back to server .env key."""
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
                    return decrypt_api_key(row.encrypted_key, settings_obj.SECRET_KEY)
                except InvalidToken:
                    # Encrypted under a different SECRET_KEY, for example after rotating it.
                    # Fall back to the server key rather than failing the request.
                    import logging
                    logging.getLogger(__name__).warning(
                        "Saved %s key for user %s cannot be decrypted with the current "
                        "SECRET_KEY. Using the server key instead; the user should save it again.",
                        provider, user_id,
                    )
    return get_api_key(provider, settings_obj)
