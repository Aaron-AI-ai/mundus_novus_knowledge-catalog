from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

# Default Ollama endpoint on the local machine. LiteLLM also honors the
# OLLAMA_API_BASE env var; we fall back to this when it is unset so the
# common "Ollama on localhost" case works with no extra configuration.
_DEFAULT_OLLAMA_API_BASE = "http://localhost:11434"


def is_litellm_model(model: str) -> bool:
    """Return True for provider-prefixed model ids that must be routed
    through LiteLLM rather than served natively by Google.

    Examples that route through LiteLLM:
        ollama_chat/qwen3-coder-next:q8_0
        ollama/qwen3-coder-next:q8_0
        openai/gpt-4o

    Bare Gemini ids ('gemini-flash-latest', 'gemini-2.5-pro') contain no
    slash and are served natively, so they return False.
    """
    return "/" in model and not model.startswith("gemini")


def _ollama_api_base(model: str) -> str | None:
    """Resolve the Ollama base URL for an ``ollama``/``ollama_chat`` model.

    Returns None for non-Ollama providers (LiteLLM resolves those itself).
    """
    provider = model.split("/", 1)[0]
    if provider in ("ollama", "ollama_chat"):
        return os.environ.get("OLLAMA_API_BASE") or _DEFAULT_OLLAMA_API_BASE
    return None


def resolve_agent_model(model: str):
    """Return the value to hand to ``google.adk.Agent(model=...)``.

    Gemini ids pass through unchanged (native ADK path). Provider-prefixed
    ids are wrapped in a ``LiteLlm`` instance so ADK drives them via LiteLLM
    (Ollama, OpenAI-compatible endpoints, etc.).
    """
    if not is_litellm_model(model):
        return model

    from google.adk.models.lite_llm import LiteLlm

    kwargs: dict[str, str] = {}
    api_base = _ollama_api_base(model)
    if api_base:
        kwargs["api_base"] = api_base
    log.debug("Routing model %s through LiteLlm (kwargs=%s)", model, kwargs)
    return LiteLlm(model=model, **kwargs)


def generate_text(model: str, prompt: str) -> str:
    """Single-shot, tool-free text generation used outside the agent loop
    (e.g. index.md description synthesis).

    Routes Gemini ids through ``google.genai`` and provider-prefixed ids
    through ``litellm.completion``. Raises on failure; callers are expected
    to handle their own fallback.
    """
    if is_litellm_model(model):
        import litellm

        kwargs: dict[str, str] = {}
        api_base = _ollama_api_base(model)
        if api_base:
            kwargs["api_base"] = api_base
        resp = litellm.completion(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            **kwargs,
        )
        return (resp["choices"][0]["message"]["content"] or "").strip()

    from google import genai

    client = genai.Client()
    response = client.models.generate_content(model=model, contents=prompt)
    return (getattr(response, "text", None) or "").strip()
