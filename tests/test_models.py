"""Tests for vendor-agnostic model loading + cost math (spelunk.agent.models).

NO network / NO real API calls. We point `load_models_config` at a tiny
synthetic yaml in `tmp_path`, assert it parses, and check `usd_cost` math on
known token counts. `load_model` is only exercised for its lazy-import wiring
(and is skipped when provider API keys are absent), so the suite stays green
fully offline.
"""
from __future__ import annotations

import os
import textwrap

import pytest

from spelunk.agent.models import (
    ModelSpec,
    load_model,
    load_models_config,
    usd_cost,
)

# A tiny synthetic config mirroring the shape of configs/models.yaml.
# Prices are USD per 1,000,000 tokens.
SYNTHETIC_YAML = textwrap.dedent(
    """
    models:
      - name: cheap-claude
        provider: anthropic
        model_id: claude-haiku-x
        tier: cheap
        price_in: 1.00
        price_out: 5.00

      - name: cheap-gpt
        provider: openai
        model_id: gpt-mini-x
        tier: cheap
        price_in: 0.25
        price_out: 2.00

      - name: frontier-claude
        provider: anthropic
        model_id: claude-opus-x
        tier: frontier
        price_in: 15.00
        price_out: 75.00
    """
)


@pytest.fixture
def config_path(tmp_path):
    """Write the synthetic yaml to a temp file and return its path."""
    p = tmp_path / "models.yaml"
    p.write_text(SYNTHETIC_YAML, encoding="utf-8")
    return p


def test_load_models_config_parses_specs(config_path):
    specs = load_models_config(config_path)
    assert isinstance(specs, dict)
    assert set(specs) == {"cheap-claude", "cheap-gpt", "frontier-claude"}
    assert all(isinstance(s, ModelSpec) for s in specs.values())


def test_load_models_config_keyed_by_name(config_path):
    specs = load_models_config(config_path)
    spec = specs["cheap-claude"]
    assert spec.name == "cheap-claude"
    assert spec.provider == "anthropic"
    assert spec.model_id == "claude-haiku-x"
    assert spec.tier == "cheap"
    assert spec.price_in == 1.00
    assert spec.price_out == 5.00


def test_load_models_config_prices_are_floats(config_path):
    specs = load_models_config(config_path)
    for spec in specs.values():
        assert isinstance(spec.price_in, float)
        assert isinstance(spec.price_out, float)


def test_load_models_config_default_path_is_real_config():
    """With no path argument, the real configs/models.yaml must parse."""
    specs = load_models_config()
    assert len(specs) >= 2
    assert all(isinstance(s, ModelSpec) for s in specs.values())


def test_load_models_config_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_models_config(tmp_path / "does-not-exist.yaml")


def test_usd_cost_basic_math(config_path):
    # cheap-claude: price_in=1.00, price_out=5.00 (per 1e6 tokens)
    # 1_000_000 prompt tokens -> 1.00 ; 1_000_000 completion tokens -> 5.00
    cost = usd_cost("cheap-claude", 1_000_000, 1_000_000, path=config_path)
    assert cost == pytest.approx(6.00)


def test_usd_cost_partial_tokens(config_path):
    # cheap-gpt: price_in=0.25, price_out=2.00
    # 500_000 prompt -> 0.125 ; 250_000 completion -> 0.50
    cost = usd_cost("cheap-gpt", 500_000, 250_000, path=config_path)
    assert cost == pytest.approx(0.125 + 0.50)


def test_usd_cost_zero_tokens(config_path):
    assert usd_cost("frontier-claude", 0, 0, path=config_path) == 0.0


def test_usd_cost_unknown_model_raises(config_path):
    with pytest.raises(KeyError):
        usd_cost("no-such-model", 100, 100, path=config_path)


def test_usd_cost_matches_explicit_formula(config_path):
    specs = load_models_config(config_path)
    spec = specs["frontier-claude"]
    pt, ct = 1234, 5678
    expected = pt / 1e6 * spec.price_in + ct / 1e6 * spec.price_out
    assert usd_cost("frontier-claude", pt, ct, path=config_path) == pytest.approx(expected)


def test_load_model_lazy_import(monkeypatch, config_path):
    """`load_model` must lazy-import init_chat_model and call it with
    `provider:model_id`. We inject a stub `langchain.chat_models` module so no
    real langchain / network / API keys are needed (works fully offline)."""
    import sys
    import types

    captured = {}

    def fake_init_chat_model(spec_str, *args, **kwargs):
        captured["spec_str"] = spec_str
        return object()

    # Build a minimal stub package: langchain -> langchain.chat_models
    pkg = types.ModuleType("langchain")
    pkg.__path__ = []  # mark as package so submodule import resolves
    submod = types.ModuleType("langchain.chat_models")
    submod.init_chat_model = fake_init_chat_model
    monkeypatch.setitem(sys.modules, "langchain", pkg)
    monkeypatch.setitem(sys.modules, "langchain.chat_models", submod)

    model = load_model("cheap-claude", path=config_path)
    assert model is not None
    assert captured["spec_str"] == "anthropic:claude-haiku-x"


def test_load_model_does_not_import_langchain_at_module_top():
    """Importing spelunk.agent.models must not require langchain to be imported."""
    import spelunk.agent.models as m

    src = m.__file__
    with open(src, encoding="utf-8") as fh:
        head = fh.read()
    # The langchain import must live inside load_model, not at module top.
    top = head.split("def load_model", 1)[0]
    assert "init_chat_model" not in top


def _langchain_available() -> bool:
    import importlib.util

    return importlib.util.find_spec("langchain") is not None


@pytest.mark.skipif(
    not (
        _langchain_available()
        and (os.getenv("ANTHROPIC_API_KEY") or os.getenv("OPENAI_API_KEY"))
    ),
    reason="langchain not installed or no provider API key set; skip real wiring",
)
def test_load_model_real_init_returns_object(config_path):
    """Only runs when langchain is installed AND a real key is present; never offline."""
    model = load_model("cheap-claude", path=config_path)
    assert model is not None
