"""Tests for agent.rungs — parsing configs/rungs.yaml into typed RungConfig objects."""
from __future__ import annotations

import pytest

from spelunk.agent.rungs import RungConfig, get_rung, load_rungs


def test_load_rungs_finds_all_three():
    rungs = load_rungs()
    assert set(rungs) == {"R0_baseline", "R1_discovery_fs", "R2_schema_rag"}


def test_load_rungs_preserves_declared_order():
    names = list(load_rungs())
    assert names == ["R0_baseline", "R1_discovery_fs", "R2_schema_rag"]


def test_r0_is_bare_dump():
    r0 = get_rung("R0_baseline")
    assert r0.schema_mode == "dump"
    assert r0.profile is False
    assert r0.rag is False


def test_r1_explores_and_profiles_without_rag():
    r1 = get_rung("R1_discovery_fs")
    assert r1.schema_mode == "explore"
    assert r1.profile is True
    assert r1.rag is False


def test_r2_adds_rag_with_top_k():
    r2 = get_rung("R2_schema_rag")
    assert r2.schema_mode == "explore"
    assert r2.profile is True
    assert r2.rag is True
    assert r2.rag_top_k == 5


def test_rag_top_k_defaults_when_omitted():
    # R0/R1 omit rag_top_k in the yaml; it must still parse with the default.
    assert get_rung("R0_baseline").rag_top_k == 5


def test_missing_config_raises():
    with pytest.raises(FileNotFoundError):
        load_rungs("configs/does_not_exist.yaml")


def test_unknown_rung_name_raises():
    with pytest.raises(KeyError):
        get_rung("R9_nonexistent")


def test_invalid_schema_mode_rejected(tmp_path):
    bad = tmp_path / "rungs.yaml"
    bad.write_text("rungs:\n  - name: bad\n    schema_mode: teleport\n", encoding="utf-8")
    with pytest.raises(Exception):  # pydantic ValidationError on the Literal
        load_rungs(bad)


def test_rungconfig_is_constructible_directly():
    rc = RungConfig(name="custom", schema_mode="explore", profile=True, rag=True, rag_top_k=3)
    assert rc.rag_top_k == 3
