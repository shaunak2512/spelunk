"""Ablation rung configuration (Wave 3).

A *rung* is one row of the additive ablation (see ``configs/rungs.yaml`` and the build
spec §3.2). Each rung toggles which harness components the agent gets:

  * ``schema_mode``  — ``"dump"``    : the full schema DDL is placed in the prompt
                       ``"explore"`` : no dump; the agent calls ``list_tables`` /
                                       ``describe_table`` to learn the schema itself.
  * ``profile``      — populate per-column stats (null %, distinct count, sample values)
                       in ``describe``.
  * ``rag``          — pre-retrieve the top-k most relevant tables for the question.
  * ``rag_top_k``    — how many tables to retrieve when ``rag`` is on.

This module is pure config: it parses the yaml into typed :class:`RungConfig` objects.
``graph.py`` consumes them to decide how to build the agent's context. Nothing here
imports an LLM or a database, so it is offline and dependency-light.
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel

DEFAULT_CONFIG_PATH = "configs/rungs.yaml"

SchemaMode = Literal["dump", "explore"]


class RungConfig(BaseModel):
    """One ablation rung from ``configs/rungs.yaml``.

    ``rag_top_k`` is only meaningful when ``rag`` is ``True``; it defaults to 5 so
    rungs that omit it (R0/R1) parse cleanly.
    """

    name: str
    schema_mode: SchemaMode
    profile: bool = False
    rag: bool = False
    rag_top_k: int = 5


def load_rungs(path: str | Path = DEFAULT_CONFIG_PATH) -> dict[str, RungConfig]:
    """Parse the rungs config yaml into a mapping of ``name -> RungConfig``.

    Order is preserved (Python dicts are insertion-ordered), so iterating the result
    yields rungs in their declared R0 → R1 → R2 sequence. Raises ``FileNotFoundError``
    if the path does not exist.
    """
    cfg_path = Path(path)
    if not cfg_path.exists():
        raise FileNotFoundError(f"rungs config not found: {cfg_path}")

    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    entries = data.get("rungs", [])
    rungs = [RungConfig(**entry) for entry in entries]
    return {rung.name: rung for rung in rungs}


def get_rung(name: str, path: str | Path = DEFAULT_CONFIG_PATH) -> RungConfig:
    """Load a single rung by name. Raises ``KeyError`` if it is not in the config."""
    return load_rungs(path)[name]
