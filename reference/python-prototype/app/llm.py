"""Thin Claude wrapper. Every call returns schema-validated JSON or raises LLMUnavailable,
so callers can fall back to the deterministic engine and record which engine produced the result."""
import json
import os

MODEL = os.environ.get("SDR_MODEL", "claude-opus-5-5")


class LLMUnavailable(Exception):
    pass


def enabled() -> bool:
    mode = os.environ.get("SDR_ENGINE", "auto").lower()
    if mode == "heuristic":
        return False
    has_creds = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
    return mode == "llm" or has_creds


_client = None


def _get_client():
    global _client
    if _client is None:
        import anthropic
        _client = anthropic.Anthropic(timeout=60.0, max_retries=1)
    return _client


def structured(system: str, user: str, schema: dict, effort: str = "low", max_tokens: int = 4000) -> dict:
    if not enabled():
        raise LLMUnavailable("LLM disabled (no credentials or SDR_ENGINE=heuristic)")
    import anthropic
    try:
        response = _get_client().beta.messages.create(
            model=MODEL,
            max_tokens=max_tokens,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        )
    except anthropic.APIConnectionError as e:
        raise LLMUnavailable(f"connection error: {e}") from e
    except anthropic.RateLimitError as e:
        raise LLMUnavailable("rate limited") from e
    except anthropic.APIStatusError as e:
        raise LLMUnavailable(f"API error {e.status_code}: {e.message}") from e
    if response.stop_reason == "refusal":
        raise LLMUnavailable("model declined the request")
    if response.stop_reason == "max_tokens":
        raise LLMUnavailable("response truncated")
    text = next((b.text for b in response.content if b.type == "text"), None)
    if text is None:
        raise LLMUnavailable("no text block in response")
    return json.loads(text)
