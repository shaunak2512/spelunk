#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Interactive chat CLI for the Spelunk database agent.

Usage (from repo root):
    python scripts/chat.py
    python scripts/chat.py --db "sample data/pitchfork_review.sqlite"
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

# Ensure UTF-8 output on Windows terminals so box-drawing characters display correctly.
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass  # Python < 3.7

# Make the spelunk package importable when run directly from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # dotenv not installed; rely on env vars already set

# ---------------------------------------------------------------------------
# ANSI colour helpers (no external deps)
# ---------------------------------------------------------------------------

_CYAN    = "\033[96m"
_YELLOW  = "\033[93m"
_GREEN   = "\033[92m"
_BLUE    = "\033[94m"
_MAGENTA = "\033[95m"
_RESET   = "\033[0m"
_BOLD    = "\033[1m"
_DIM     = "\033[2m"


def _indent(text: str, prefix: str = "  ") -> str:
    return "\n".join(prefix + line for line in text.splitlines())


def _print_thinking(text: str) -> None:
    print(f"\n{_BLUE}{_BOLD}╔═ thinking ══════════════════════════════════╗{_RESET}")
    print(_indent(text, f"  {_BLUE}{_DIM}") + _RESET)
    print(f"{_BLUE}{_BOLD}╚═════════════════════════════════════════════╝{_RESET}")


def _print_tool_call(name: str, args: dict) -> None:
    args_str = json.dumps(args, indent=2)
    print(f"\n{_YELLOW}{_BOLD}▶ tool call: {name}{_RESET}")
    print(_indent(args_str, f"  {_YELLOW}{_DIM}") + _RESET)


def _print_tool_result(name: str, result: str) -> None:
    MAX = 500
    preview = result if len(result) <= MAX else result[:MAX - 3] + "..."
    print(f"{_GREEN}◀ tool result: {name}{_RESET}")
    print(_indent(preview, f"  {_GREEN}{_DIM}") + _RESET)


def _print_assistant(text: str) -> None:
    print(f"\n{_MAGENTA}{_BOLD}Assistant:{_RESET}\n{text}\n")


def _print_separator() -> None:
    print(f"{_DIM}{'─' * 60}{_RESET}")


# ---------------------------------------------------------------------------
# Model catalogue
# ---------------------------------------------------------------------------

MODELS: list[dict[str, Any]] = [
    # Anthropic — standard
    {
        "label":    "Claude Haiku 4.5       (fast · cheap · Anthropic)",
        "provider": "anthropic",
        "model_id": "claude-haiku-4-5",
        "thinking": False,
    },
    {
        "label":    "Claude Sonnet 4.6      (balanced · Anthropic)",
        "provider": "anthropic",
        "model_id": "claude-sonnet-4-6",
        "thinking": False,
    },
    {
        "label":    "Claude Sonnet 4.6 + Extended Thinking  (Anthropic)",
        "provider": "anthropic",
        "model_id": "claude-sonnet-4-6",
        "thinking": True,
    },
    {
        "label":    "Claude Opus 4.8        (frontier · Anthropic)",
        "provider": "anthropic",
        "model_id": "claude-opus-4-8",
        "thinking": False,
    },
    {
        "label":    "Claude Opus 4.8 + Extended Thinking    (Anthropic)",
        "provider": "anthropic",
        "model_id": "claude-opus-4-8",
        "thinking": True,
    },
    # OpenAI — standard
    {
        "label":    "GPT-4o mini            (fast · cheap · OpenAI)",
        "provider": "openai",
        "model_id": "gpt-4o-mini",
        "thinking": False,
    },
    {
        "label":    "GPT-4o                 (capable · OpenAI)",
        "provider": "openai",
        "model_id": "gpt-4o",
        "thinking": False,
    },
    # OpenAI — reasoning (thinking happens server-side)
    {
        "label":    "o4-mini                (reasoning · OpenAI)",
        "provider": "openai",
        "model_id": "o4-mini",
        "thinking": False,
        "reasoning": True,
    },
    {
        "label":    "o3                     (frontier reasoning · OpenAI)",
        "provider": "openai",
        "model_id": "o3",
        "thinking": False,
        "reasoning": True,
    },
]


def _pick_model() -> dict[str, Any]:
    width = max(len(m["label"]) for m in MODELS) + 6
    border = "─" * width

    print(f"\n{_BOLD}┌{border}┐{_RESET}")
    print(f"{_BOLD}│  {'Available models':<{width - 2}}│{_RESET}")
    print(f"{_BOLD}├{border}┤{_RESET}")
    for i, m in enumerate(MODELS, 1):
        print(f"{_BOLD}│{_RESET}  {i:>2}. {m['label']:<{width - 6}}{_BOLD}│{_RESET}")
    print(f"{_BOLD}└{border}┘{_RESET}")

    while True:
        raw = input(f"\nSelect model [1–{len(MODELS)}]: ").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(MODELS):
            return MODELS[int(raw) - 1]
        print(f"  Please enter a number between 1 and {len(MODELS)}.")


def _build_model(spec: dict[str, Any]) -> Any:
    """Instantiate a LangChain chat model from a spec dict."""
    if spec["provider"] == "anthropic":
        from langchain_anthropic import ChatAnthropic  # type: ignore[import]

        kwargs: dict[str, Any] = {"model": spec["model_id"]}
        if spec.get("thinking"):
            kwargs["thinking"] = {"type": "enabled", "budget_tokens": 8000}
        return ChatAnthropic(**kwargs)

    # OpenAI
    from langchain_openai import ChatOpenAI  # type: ignore[import]

    kwargs = {"model": spec["model_id"]}
    if spec.get("reasoning"):
        # o-series models accept reasoning_effort; "medium" is a sensible default.
        kwargs["model_kwargs"] = {"reasoning_effort": "medium"}
    return ChatOpenAI(**kwargs)


# ---------------------------------------------------------------------------
# Content-block parsing
# ---------------------------------------------------------------------------

def _parse_content(content: Any) -> tuple[list[str], str]:
    """Split AIMessage content into (thinking_blocks, text).

    Anthropic extended-thinking returns content as a list of typed blocks:
      {"type": "thinking", "thinking": "..."}
      {"type": "text",     "text":     "..."}
    Regular string content is returned as-is with an empty thinking list.
    """
    if isinstance(content, str):
        return [], content

    thinking: list[str] = []
    text_parts: list[str] = []

    for block in content:
        if not isinstance(block, dict):
            text_parts.append(str(block))
            continue
        btype = block.get("type", "")
        if btype == "thinking":
            t = block.get("thinking", "")
            if t:
                thinking.append(t)
        elif btype == "text":
            t = block.get("text", "")
            if t:
                text_parts.append(t)

    return thinking, "\n".join(text_parts)


# ---------------------------------------------------------------------------
# ReAct turn runner
# ---------------------------------------------------------------------------

def _run_turn(
    messages: list[Any],
    bound_model: Any,
    tools_by_name: dict[str, Any],
    is_reasoning_model: bool = False,
    max_steps: int = 20,
) -> str:
    """Drive one conversational turn through the ReAct loop.

    Mutates *messages* in-place (appends AI and ToolMessages).
    Returns the final text response from the model.
    """
    from langchain_core.messages import AIMessage, ToolMessage  # noqa: F401

    for _step in range(max_steps):
        ai: AIMessage = bound_model.invoke(messages)
        messages.append(ai)

        # ---- display thinking (Anthropic extended thinking) ----
        thinking_list, text = _parse_content(ai.content)
        for chunk in thinking_list:
            _print_thinking(chunk)

        # ---- OpenAI reasoning models: show token hint instead ----
        if is_reasoning_model:
            usage = getattr(ai, "usage_metadata", None) or {}
            details = usage.get("completion_token_details", {}) or {}
            rt = details.get("reasoning_tokens")
            if rt:
                print(f"\n{_BLUE}{_DIM}[reasoning: {rt} tokens used server-side]{_RESET}")

        # ---- tool calls ----
        tool_calls = getattr(ai, "tool_calls", None) or []
        if not tool_calls:
            return text

        for call in tool_calls:
            name    = call.get("name", "")
            args    = call.get("args", {}) or {}
            call_id = call.get("id")

            _print_tool_call(name, args)

            if name in tools_by_name:
                try:
                    result = tools_by_name[name].invoke(args)
                except Exception as err:
                    result = json.dumps({"error": f"{type(err).__name__}: {err}"})
            else:
                result = json.dumps({"error": f"unknown tool: {name!r}"})

            _print_tool_result(name, result)
            messages.append(ToolMessage(content=result, tool_call_id=call_id))

    return text or "(max steps reached)"


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_SYSTEM = """You are a helpful data analyst with access to a SQLite database.

Use the provided tools to explore the schema and run queries before answering.
Never fabricate data — always query to verify. Prefer concise, direct answers
unless the user asks for detail.

Tools available:
  list_tables   — list all tables and views
  describe_table — inspect columns, keys, sample rows, and statistics
  run_query     — execute a read-only SELECT query"""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Chat with the Spelunk database agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--db",
        default=str(Path(__file__).resolve().parent.parent / "sample data" / "pitchfork_review.sqlite"),
        metavar="PATH",
        help="Path to the SQLite database (default: sample data/pitchfork_review.sqlite)",
    )
    cli_args = parser.parse_args()

    db_path = Path(cli_args.db)
    if not db_path.exists():
        print(f"Error: database not found: {db_path}", file=sys.stderr)
        sys.exit(1)

    # ---- model selection ------------------------------------------------
    print(f"\n{_BOLD}{_CYAN}Spelunk Database Chat{_RESET}")
    print(f"Database: {_DIM}{db_path}{_RESET}")

    model_spec = _pick_model()
    is_reasoning = bool(model_spec.get("reasoning"))

    print(f"\nLoading {_BOLD}{model_spec['label'].strip()}{_RESET} …")
    model = _build_model(model_spec)

    # ---- database + tools -----------------------------------------------
    from spelunk.core.connection import connect
    from spelunk.agent.tools import make_tools

    dsn = "sqlite:///" + db_path.resolve().as_posix()
    engine = connect(dsn)
    tools = [t for t in make_tools(engine, profile=True) if t.name != "submit_sql"]
    tools_by_name = {t.name: t for t in tools}
    bound_model = model.bind_tools(tools)

    # ---- conversation ---------------------------------------------------
    from langchain_core.messages import HumanMessage, SystemMessage

    messages: list[Any] = [SystemMessage(content=_SYSTEM)]

    print(f"\n{_GREEN}Ready — type your question, or 'exit' to quit.{_RESET}")
    if model_spec.get("thinking"):
        print(f"{_DIM}Extended thinking is enabled; thinking blocks will be shown.{_RESET}")
    if is_reasoning:
        print(f"{_DIM}Reasoning model: thinking happens server-side (token count shown).{_RESET}")
    print()

    while True:
        _print_separator()
        try:
            user_input = input(f"{_CYAN}{_BOLD}You:{_RESET} ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye.")
            break

        if not user_input:
            continue
        if user_input.lower() in {"exit", "quit", "q", ":q"}:
            print("Goodbye.")
            break

        messages.append(HumanMessage(content=user_input))

        try:
            response = _run_turn(
                messages,
                bound_model,
                tools_by_name,
                is_reasoning_model=is_reasoning,
            )
            _print_assistant(response or "(no text response)")
        except KeyboardInterrupt:
            print("\n(interrupted)\n")
        except Exception as err:
            print(f"\n{_YELLOW}Error: {err}{_RESET}\n")
            import traceback
            traceback.print_exc()


if __name__ == "__main__":
    main()
