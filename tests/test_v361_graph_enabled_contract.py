"""v3.6.1 — ``graph_enabled`` reads the legacy contract, not the rollout default.

Why this file exists: ``MemoryConfig.graph_enabled`` used to be spelled
``graph_mode != DEFAULT_GRAPH_MODE``. That was correct only by coincidence
when the rollout default was ``"legacy"``. The two are different ideas:

  * ``"legacy"`` is the **contract** for "nothing graph-shaped is switched on".
    Every caller that branches on ``graph_enabled`` means exactly that.
  * ``DEFAULT_GRAPH_MODE`` is a **rollout choice** — which mode an operator who
    declared nothing lands in.

Tying the predicate to a non-legacy default silently inverts callers' questions:
an explicitly graph-enabled config reports disabled, and legacy reports enabled.
These tests pin the predicate to the contract so that failure cannot happen quietly.

The rollout default is ``"graph"`` and
``test_v360_graph_integration_config.py`` pins that resolution. The
monkeypatched-default cases below pass an *explicit* ``graph_mode``
precisely because they are about the predicate, not about the default.

Every config here is built against ``tmp_path`` with an explicit home. Nothing
reads the real memory home, the real config.yaml or any user memory data.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from layered_memory_mcp import config as config_module
from layered_memory_mcp.config import DEFAULT_GRAPH_MODE, GRAPH_MODES, MemoryConfig

_GRAPH_ENV_VARS = ("LAYERED_MEMORY_GRAPH_MODE", "LAYERED_MEMORY_GRAPH_DB_PATH")


@pytest.fixture(autouse=True)
def _clean_graph_env(monkeypatch):
    """No inherited graph env var may decide what these tests observe."""
    for var in _GRAPH_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def _config(tmp_path: Path, **kwargs) -> MemoryConfig:
    home = tmp_path / "graph-enabled-home"
    home.mkdir(parents=True, exist_ok=True)
    return MemoryConfig(home=str(home), **kwargs)


# ---------------------------------------------------------------------------
# The contract itself
# ---------------------------------------------------------------------------

def test_legacy_is_the_only_mode_that_means_disabled(tmp_path):
    config = _config(tmp_path, graph_mode="legacy")

    assert config.graph_enabled is False
    assert config.graph_recall_enabled is False
    assert config.graph_write_enabled is False
    assert config.graph_shadow_enabled is False


@pytest.mark.parametrize("mode", ["shadow", "graph_recall", "graph_write", "graph"])
def test_every_non_legacy_mode_means_enabled(tmp_path, mode):
    assert _config(tmp_path, graph_mode=mode).graph_enabled is True


def test_shadow_counts_as_enabled_without_being_authoritative(tmp_path):
    """Shadow is observed, so it *is* switched on — it just decides nothing."""
    config = _config(tmp_path, graph_mode="shadow")

    assert config.graph_enabled is True
    assert config.graph_shadow_enabled is True
    assert (config.graph_recall_enabled, config.graph_write_enabled) == (False, False)


def test_graph_is_enabled_and_authoritative_on_both_paths(tmp_path):
    config = _config(tmp_path, graph_mode="graph")

    assert config.graph_enabled is True
    assert (config.graph_recall_enabled, config.graph_write_enabled) == (True, True)
    assert config.graph_shadow_enabled is False


def test_the_contract_covers_every_declared_mode(tmp_path):
    """No mode may be left without a declared side of the switch."""
    enabled = {
        mode: _config(tmp_path, graph_mode=mode).graph_enabled for mode in GRAPH_MODES
    }
    assert enabled == {
        "legacy": False,
        "shadow": True,
        "graph_recall": True,
        "graph_write": True,
        "graph": True,
    }


# ---------------------------------------------------------------------------
# Independence from the rollout default
# ---------------------------------------------------------------------------
#
# The default is *not* changed here: each case passes an explicit ``graph_mode``
# and only monkeypatches the module constant, to show the predicate no longer
# reads it.

def test_the_rollout_default_is_graph():
    """Guard for the cases below: they are about the predicate, not the default."""
    assert DEFAULT_GRAPH_MODE == "graph"


@pytest.mark.parametrize("default_mode", GRAPH_MODES)
def test_legacy_stays_disabled_when_the_default_is_patched(
    tmp_path, monkeypatch, default_mode
):
    monkeypatch.setattr(config_module, "DEFAULT_GRAPH_MODE", default_mode)

    config = _config(tmp_path, graph_mode="legacy")

    assert config.graph_mode == "legacy"
    # The old spelling (``!= DEFAULT_GRAPH_MODE``) fails for non-legacy defaults.
    assert config.graph_enabled is False


@pytest.mark.parametrize("default_mode", GRAPH_MODES)
@pytest.mark.parametrize("mode", ["shadow", "graph_recall", "graph_write", "graph"])
def test_enabled_modes_stay_enabled_when_the_default_is_patched(
    tmp_path, monkeypatch, mode, default_mode
):
    monkeypatch.setattr(config_module, "DEFAULT_GRAPH_MODE", default_mode)

    config = _config(tmp_path, graph_mode=mode)

    assert config.graph_mode == mode
    # The old spelling would say False when the explicit mode equals the default.
    assert config.graph_enabled is True


@pytest.mark.parametrize("default_mode", GRAPH_MODES)
def test_the_other_predicates_are_also_default_independent(
    tmp_path, monkeypatch, default_mode
):
    monkeypatch.setattr(config_module, "DEFAULT_GRAPH_MODE", default_mode)

    legacy = _config(tmp_path, graph_mode="legacy")
    shadow = _config(tmp_path, graph_mode="shadow")

    assert (
        legacy.graph_recall_enabled,
        legacy.graph_write_enabled,
        legacy.graph_shadow_enabled,
    ) == (False, False, False)
    assert (
        shadow.graph_recall_enabled,
        shadow.graph_write_enabled,
        shadow.graph_shadow_enabled,
    ) == (False, False, True)


def test_graph_enabled_is_the_disjunction_of_nothing_being_legacy(tmp_path):
    """``graph_enabled`` must agree with the three specific predicates.

    Anything enabled on a specific path implies the general switch is on; the
    converse does not hold (shadow enables no specific path), which is why the
    assertion is one-directional.
    """
    for mode in GRAPH_MODES:
        config = _config(tmp_path, graph_mode=mode)
        if (
            config.graph_recall_enabled
            or config.graph_write_enabled
            or config.graph_shadow_enabled
        ):
            assert config.graph_enabled is True
