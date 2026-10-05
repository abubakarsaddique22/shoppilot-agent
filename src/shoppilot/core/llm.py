"""The chat model for the agents (Step J). The model comes only from .env, so changing it needs no code edit.

    SHOP_LLM_PROVIDER=groq            # groq | google
    SHOP_LLM_MODEL=openai/gpt-oss-120b
    SHOP_LLM_API_KEY=...              # Groq key
    SHOP_GOOGLE_API_KEY=...           # Gemini key (Google AI Studio)
"""
from langchain_core.language_models import BaseChatModel

from shoppilot.core.config import settings
from shoppilot.core.errors import ConfigError


def get_llm(temperature: float = 0.0) -> BaseChatModel:
    """Temperature 0 for deciding and checking; a little higher (0.2 to 0.5) is fine for reply and listing text."""
    provider = settings.llm_provider.strip().lower()
    model = settings.llm_model.strip()
    if model in ("", "set-in-env"):
        raise ConfigError("SHOP_LLM_MODEL is not set in .env")

    if provider == "groq":
        from langchain_groq import ChatGroq

        return ChatGroq(model=model, api_key=settings.llm_api_key or None, temperature=temperature, timeout=settings.run_timeout_s)
    if provider == "google":
        from langchain_google_genai import ChatGoogleGenerativeAI

        if not settings.google_api_key:
            raise ConfigError("SHOP_GOOGLE_API_KEY is not set in .env")
        return ChatGoogleGenerativeAI(model=model, api_key=settings.google_api_key, temperature=temperature, max_retries=6)
    raise ConfigError(f"unknown SHOP_LLM_PROVIDER: {provider!r} (use groq or google)")
