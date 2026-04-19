"""Minimal Gemini adapter to present a compatible interface to
`translate_to_c.py` which expects an `Anthropic()`-like client.

Uses the new `google-genai` SDK (pip install google-genai).
"""
from __future__ import annotations

import os

try:
    from google import genai
except Exception as exc:
    raise RuntimeError(
        "Install the Gemini SDK: pip install google-genai"
    ) from exc

# Default model when the caller passes a non-Gemini name (e.g. 'claude-opus-4-7')
_DEFAULT_MODEL = "gemini-2.0-flash-lite"


class _Block:
    def __init__(self, t: str, text: str) -> None:
        self.type = t
        self.text = text


class _Resp:
    def __init__(self, blocks: list[_Block]) -> None:
        self.content = blocks


def _resolve_model(raw: str | None) -> str:
    """Pick a Gemini model name from env override, caller arg, or default."""
    env = os.environ.get("GEMINI_MODEL")
    if env:
        return env
    if isinstance(raw, str) and "gemini" in raw.lower():
        return raw
    return _DEFAULT_MODEL


class GeminiClient:
    """Drop-in replacement for the Anthropic client used by translate_to_c."""

    def __init__(self) -> None:
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY not set")
        self._client = genai.Client(api_key=api_key)
        self.messages = self._Messages(self._client)

    class _Messages:
        def __init__(self, client):
            self._client = client

        def create(self, model, max_tokens, system, messages):
            # Assemble system instruction
            sys_texts = []
            for s in (system or []):
                if isinstance(s, dict):
                    sys_texts.append(s.get("text", ""))
                else:
                    sys_texts.append(str(s))
            sys_text = "\n".join(sys_texts)

            # Assemble user content
            user_text = "\n\n".join(
                m.get("content", "") for m in messages
            )

            model_name = _resolve_model(model)
            max_out = int(
                os.environ.get("GEMINI_MAX_TOKENS", max_tokens or 8192)
            )

            resp = self._client.models.generate_content(
                model=model_name,
                contents=user_text,
                config={
                    "system_instruction": sys_text,
                    "max_output_tokens": max_out,
                    "temperature": 0.0,
                },
            )

            # Extract text
            try:
                text = resp.text
            except Exception:
                text = str(resp)

            return _Resp([_Block("text", text or "")])
