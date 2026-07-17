"""LLM gateway — the only module that speaks to a model provider.

Single responsibility (ADR-0004 boundary table): wrap LiteLLM (OpenRouter
first, swappable) behind a small async interface — chat, stream, embeddings,
tool-calls. **Nobody else may import LiteLLM or call a model/embeddings
endpoint.** Callers receive domain types (``app.domain.llm``), never a LiteLLM
object, so swapping the provider is a change confined to this module.
"""

from app.llm.gateway import LLMGateway, LlmProviderError, aclose_litellm_clients

__all__ = ["LLMGateway", "LlmProviderError", "aclose_litellm_clients"]
