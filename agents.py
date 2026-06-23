"""
agents.py - Agent call abstraction layer
========================================
Two public functions:
  - call_reasoning_agent(role, context) -> dict
  - run_coding_agent(card, role)        -> dict  (M2)

Provider is controlled by company.yaml:
  reasoning_provider: "gemini"   # free tier, no billing needed
  reasoning_provider: "claude"   # Anthropic API, needs paid account
"""

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger("agents")

CONFIG_PATH = Path(__file__).parent / "company.yaml"
PROMPTS_DIR = Path(__file__).parent / "prompts"


def _load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _load_prompt(role: str) -> str:
    path = PROMPTS_DIR / f"{role}.md"
    if path.exists():
        return path.read_text(encoding="utf-8")
    raise FileNotFoundError(f"Prompt file not found: {path}")


# ------------------------------------------------------------------ #
#  Reasoning agent (Spec, Review) - provider-switchable               #
# ------------------------------------------------------------------ #

def call_reasoning_agent(role: str, context: dict, retry: int = 1) -> dict:
    """
    Call a reasoning agent (Spec or Review).
    Provider is determined by company.yaml reasoning_provider.

    Args:
        role:    'spec' | 'review'
        context: dict passed to the model as user message
        retry:   number of retries on JSON parse failure (default 1)

    Returns:
        Parsed JSON dict
    """
    cfg = _load_config()
    provider = cfg.get("reasoning_provider", "gemini").lower()
    model = cfg.get("reasoning_model", "gemini-2.5-flash")
    system = _load_prompt(role)

    log.info("call_reasoning_agent role=%s provider=%s model=%s", role, provider, model)

    for attempt in range(retry + 1):
        try:
            if provider == "gemini":
                raw = _call_gemini(model, system, context)
            elif provider == "claude":
                raw = _call_claude(model, system, context)
            else:
                raise ValueError(f"Unknown reasoning_provider: {provider!r}. Use 'gemini' or 'claude'.")

            return _parse_json_strict(raw)

        except (json.JSONDecodeError, ValueError) as e:
            if attempt < retry:
                log.warning("JSON parse failed (attempt %d), retrying: %s", attempt + 1, e)
                time.sleep(1)
            else:
                raise RuntimeError(
                    f"call_reasoning_agent failed after {retry + 1} attempts: {e}"
                ) from e


def _call_gemini(model: str, system: str, context: dict) -> str:
    """Call Gemini API (google-genai SDK). Requires GEMINI_API_KEY in environment."""
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        raise ImportError(
            "google-genai not installed. Run: pip install google-genai"
        )

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise EnvironmentError("GEMINI_API_KEY not set. Get a free key at https://aistudio.google.com")

    client = genai.Client(api_key=api_key)
    full_system = system + "\n\nOutput only valid JSON. No markdown fences, no explanation."
    user_msg = json.dumps(context, ensure_ascii=False)

    response = client.models.generate_content(
        model=model,
        contents=user_msg,
        config=types.GenerateContentConfig(
            system_instruction=full_system,
        ),
    )
    return response.text


def _call_claude(model: str, system: str, context: dict) -> str:
    """Call Anthropic Claude API. Requires ANTHROPIC_API_KEY in environment."""
    try:
        import anthropic
    except ImportError:
        raise ImportError(
            "anthropic not installed. Run: pip install anthropic"
        )

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError("ANTHROPIC_API_KEY not set.")

    client = anthropic.Anthropic(api_key=api_key)
    full_system = system + "\n\nOutput only valid JSON. No markdown fences, no explanation."
    user_msg = json.dumps(context, ensure_ascii=False)

    resp = client.messages.create(
        model=model,
        max_tokens=2000,
        system=full_system,
        messages=[{"role": "user", "content": user_msg}],
    )
    return resp.content[0].text


# ------------------------------------------------------------------ #
#  Coding agent (Test, Code) - always uses claude -p (M2)             #
# ------------------------------------------------------------------ #

def run_coding_agent(card: dict, role: str) -> dict:
    """
    Run coding agent inside sandbox container via claude -p.
    Covered by Claude Pro subscription (not API billing).

    Args:
        card: card dict (needs id, branch)
        role: 'test' | 'code'

    Returns:
        {'exit_code': int, 'stdout': str, 'stderr': str}
    """
    cfg = _load_config()
    model = cfg.get("coding_model", "claude-sonnet-4-6")
    max_turns = cfg.get("limits", {}).get("max_turns", 25)
    timeout = cfg.get("limits", {}).get("worker_timeout_sec", 1800)

    log.info(
        "[M0-STUB] run_coding_agent card=%s role=%s model=%s max_turns=%d",
        card["id"], role, model, max_turns,
    )
    # M2 implementation:
    # prompt = build_prompt(card, role)
    # cmd = [
    #     "claude", "-p", prompt,
    #     "--allowedTools", "Read,Edit,Bash",
    #     "--permission-mode", "acceptEdits",
    #     "--max-turns", str(max_turns),
    #     "--output-format", "json",
    #     "--cwd", f"/work/{card['branch']}",
    #     "--model", model,
    # ]
    # return run_in_container(image="ai-company-worker", cmd=cmd, timeout_sec=timeout, ...)
    raise NotImplementedError("run_coding_agent: implemented in M2")


# ------------------------------------------------------------------ #
#  Utilities                                                           #
# ------------------------------------------------------------------ #

def _parse_json_strict(text: str) -> dict:
    """Parse JSON strictly. Strip markdown fences if present."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        end = -1 if lines[-1].strip() == "```" else len(lines)
        text = "\n".join(lines[1:end])
    return json.loads(text)
