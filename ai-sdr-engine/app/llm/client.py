# app/llm/client.py - OpenAI client factory (ChatGPT-subscription or API key) + Agents SDK runner helper
from __future__ import annotations

import json
import time
from typing import Any, Optional, Type, TypeVar

from agents import Agent, ModelSettings, OpenAIResponsesModel, Runner, set_tracing_disabled
from openai import AsyncOpenAI
from openai.types.shared import Reasoning
from pydantic import BaseModel

from app import config

T = TypeVar("T", bound=BaseModel)

set_tracing_disabled(True)


class LLMUnavailable(RuntimeError):
    """Raised when no model backend can serve the call; callers fall back to fixture/human review."""


# Build an AsyncOpenAI client for the configured backend (re-reads Codex auth each call so refreshed tokens are picked up)
def make_client() -> AsyncOpenAI:
    if config.LLM_BACKEND == "api_key":
        return AsyncOpenAI(api_key=config.OPENAI_API_KEY)
    try:
        tokens = json.loads(config.CODEX_AUTH_PATH.read_text())["tokens"]
    except Exception as exc:
        raise LLMUnavailable(f"ChatGPT auth not found at {config.CODEX_AUTH_PATH}: {exc}") from exc
    return AsyncOpenAI(
        base_url=config.CHATGPT_BASE_URL,
        api_key=tokens["access_token"],
        default_headers={
            "chatgpt-account-id": tokens["account_id"],
            "OpenAI-Beta": "responses=experimental",
            "originator": "codex_cli_rs",
        },
    )


# Model settings compatible with both backends (ChatGPT backend requires store=False)
def model_settings(effort: str = "low") -> ModelSettings:
    return ModelSettings(store=False, reasoning=Reasoning(effort=effort))


# Build an Agents SDK Agent bound to our client/model
def build_agent(
    name: str,
    instructions: str,
    output_type: Optional[Type[BaseModel]] = None,
    model: Optional[str] = None,
    tools: Optional[list] = None,
    effort: str = "low",
) -> Agent:
    return Agent(
        name=name,
        instructions=instructions,
        model=OpenAIResponsesModel(model or config.GENERATION_MODEL, make_client()),
        model_settings=model_settings(effort),
        output_type=output_type,
        tools=tools or [],
    )


# Run an agent to completion via streaming (ChatGPT backend is stream-only) and return (output, meta)
async def run_agent(agent: Agent, user_input: str | list[dict[str, Any]], max_turns: int = 6) -> tuple[Any, dict]:
    started = time.perf_counter()
    try:
        result = Runner.run_streamed(agent, user_input, max_turns=max_turns)
        async for _ in result.stream_events():
            pass
    except Exception as exc:
        raise LLMUnavailable(f"{type(exc).__name__}: {str(exc)[:300]}") from exc
    usage = None
    try:
        u = result.context_wrapper.usage
        usage = {"input_tokens": u.input_tokens, "output_tokens": u.output_tokens, "requests": u.requests}
    except Exception:
        pass
    meta = {
        "model": agent.model.model if hasattr(agent.model, "model") else str(agent.model),
        "backend": config.LLM_BACKEND,
        "durationMs": int((time.perf_counter() - started) * 1000),
        "usage": usage,
    }
    return result.final_output, meta


# One-shot structured call: instructions + input -> validated Pydantic object
async def structured(
    name: str,
    instructions: str,
    user_input: str,
    output_type: Type[T],
    model: Optional[str] = None,
    effort: str = "low",
) -> tuple[T, dict]:
    agent = build_agent(name, instructions, output_type=output_type, model=model, effort=effort)
    out, meta = await run_agent(agent, user_input)
    return out, meta
