"""Model gateway — the only module that speaks to a model provider.

Single responsibility (ADR-0004 boundary table): wrap LiteLLM (OpenRouter
first, swappable) behind a small async interface for chat/embeddings/tools, plus
ADR-0023's narrow direct OpenRouter speech-to-text adapter while pinned LiteLLM
cannot preserve diarization. **Nobody else may import LiteLLM or call a model
endpoint.** Callers receive domain types (``app.domain.llm``), never vendor
objects, so swapping a provider remains confined to this module.
"""

from app.llm.gateway import LLMGateway, LlmProviderError, aclose_litellm_clients
from app.llm.openrouter_stt import InvalidTranscriptionResponse, OpenRouterTranscriber

__all__ = [
    "InvalidTranscriptionResponse",
    "LLMGateway",
    "LlmProviderError",
    "OpenRouterTranscriber",
    "aclose_litellm_clients",
]
