"""Vendor-agnostic model loading + cost math, driven entirely by configs/models.yaml.

No model ids or prices are hardcoded here — they all come from the yaml. Adding a
new vendor/model is a config edit, not a code change.

langchain is imported *lazily* inside `load_model` (not at module top) so that
importing this module and running the config / cost-math tests needs no langchain
install and no network access.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import yaml
from pydantic import BaseModel

if TYPE_CHECKING:  # pragma: no cover - typing only, not imported at runtime
    from langchain_core.language_models.chat_models import BaseChatModel

DEFAULT_CONFIG_PATH = "configs/models.yaml"


class ModelSpec(BaseModel):
    """One benchmark model entry from configs/models.yaml.

    Prices are USD per 1,000,000 tokens.
    """

    name: str
    provider: str
    model_id: str
    tier: str
    price_in: float
    price_out: float


def load_models_config(path: str | Path = DEFAULT_CONFIG_PATH) -> dict[str, ModelSpec]:
    """Parse the models config yaml into a mapping of `name -> ModelSpec`.

    Raises FileNotFoundError if the path does not exist.
    """
    cfg_path = Path(path)
    if not cfg_path.exists():
        raise FileNotFoundError(f"models config not found: {cfg_path}")

    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    entries = data.get("models", [])
    specs = [ModelSpec(**entry) for entry in entries]
    return {spec.name: spec for spec in specs}


def load_model(name: str, path: str | Path = DEFAULT_CONFIG_PATH) -> "BaseChatModel":
    """Load a chat model by config name via LangChain's vendor-agnostic factory.

    langchain is imported lazily here so that merely importing this module (and
    running config/cost tests) requires neither langchain nor network access.

    Raises KeyError if `name` is not present in the config.
    """
    from langchain.chat_models import init_chat_model  # lazy: keeps import/tests offline

    spec = load_models_config(path)[name]
    return init_chat_model(f"{spec.provider}:{spec.model_id}")


def usd_cost(
    name: str,
    prompt_tokens: int,
    completion_tokens: int,
    path: str | Path = DEFAULT_CONFIG_PATH,
) -> float:
    """USD cost of a call, using prices (per 1e6 tokens) from the config.

    cost = prompt_tokens / 1e6 * price_in + completion_tokens / 1e6 * price_out

    Raises KeyError if `name` is not present in the config.
    """
    spec = load_models_config(path)[name]
    return prompt_tokens / 1e6 * spec.price_in + completion_tokens / 1e6 * spec.price_out
