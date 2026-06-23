"""Turn ``results.csv`` into the headline result of the harness.

``results.csv`` has one row per ``(model, rung, question)`` run — columns are
:data:`spelunk.eval.schemas.RESULTS_CSV_COLUMNS`, rows validate against
:class:`spelunk.eval.schemas.RunResult`.

The headline story Spelunk tells: *can a cheap model, given progressively more
scaffolding (rungs), match a frontier model's zero-scaffold (R0) accuracy?* So the
headline chart is grouped bars of the cheap models across rungs, with each
frontier-tier model's R0 score drawn as a horizontal **reference line** — the
"parity bar" the cheap models are trying to clear.

A *frontier* model is one that was only evaluated at the baseline rung ``R0_*``
(it needs no scaffolding); a *cheap* model is everything else. This is inferred
from the data rather than hard-coded against ``configs/models.yaml`` so the module
stays decoupled from the (orchestrator-owned) config files.
"""
from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # headless backend — must be set before pyplot is imported.

import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

from .schemas import RESULTS_CSV_COLUMNS  # noqa: E402

__all__ = [
    "load_results",
    "accuracy_by_rung",
    "cost_per_correct",
    "plot_headline",
    "plot_cost",
]


def load_results(csv_path) -> pd.DataFrame:
    """Load ``results.csv`` into a DataFrame with the frozen column order.

    ``ex_correct`` is coerced to a real boolean dtype (CSV round-trips it as the
    strings ``"True"`` / ``"False"``), so downstream ``.mean()`` gives a 0..1 rate.
    """
    df = pd.read_csv(csv_path)
    # Keep only known columns, in the frozen order (ignore any stray extras).
    cols = [c for c in RESULTS_CSV_COLUMNS if c in df.columns]
    df = df[cols]
    if "ex_correct" in df.columns:
        df["ex_correct"] = _coerce_bool(df["ex_correct"])
    return df


def _coerce_bool(s: pd.Series) -> pd.Series:
    """Coerce a (possibly string) column into a clean boolean Series."""
    if s.dtype == bool:
        return s
    return (
        s.astype(str)
        .str.strip()
        .str.lower()
        .map({"true": True, "1": True, "1.0": True, "false": False, "0": False, "0.0": False})
        .astype(bool)
    )


def accuracy_by_rung(df: pd.DataFrame) -> pd.DataFrame:
    """Mean execution accuracy, index = model, columns = rung.

    Cells where a model never ran a given rung are ``NaN`` (not 0) — frontier
    models only ran ``R0``, and a missing run is not a wrong answer.
    """
    acc = (
        df.assign(ex_correct=_coerce_bool(df["ex_correct"]).astype(float))
        .pivot_table(index="model", columns="rung", values="ex_correct", aggfunc="mean")
    )
    acc.index.name = "model"
    acc.columns.name = "rung"
    return acc


def cost_per_correct(df: pd.DataFrame) -> pd.DataFrame:
    """Total USD cost divided by number of correct answers, per model.

    Returns columns ``total_cost``, ``n_correct``, ``cost_per_correct`` (index =
    model). A model with zero correct answers yields ``NaN`` (never ``inf``) so it
    sorts/plots sanely rather than dominating the axis.
    """
    correct = _coerce_bool(df["ex_correct"])
    grouped = df.assign(_correct=correct.astype(int)).groupby("model", sort=True)
    out = grouped.agg(total_cost=("usd_cost", "sum"), n_correct=("_correct", "sum"))
    out["cost_per_correct"] = out["total_cost"] / out["n_correct"].where(out["n_correct"] > 0)
    out.index.name = "model"
    return out


def _split_models(df: pd.DataFrame) -> tuple[list[str], list[str]]:
    """Split models into (cheap, frontier).

    Frontier = ran *only* the baseline rung (rung name starting ``R0``); cheap =
    everything else (i.e. anything climbing the ladder past R0).
    """
    rungs_by_model = df.groupby("model")["rung"].apply(lambda s: set(s.unique()))
    frontier, cheap = [], []
    for model, rungs in rungs_by_model.items():
        if rungs and all(r.startswith("R0") for r in rungs):
            frontier.append(model)
        else:
            cheap.append(model)
    return sorted(cheap), sorted(frontier)


def plot_headline(df: pd.DataFrame, out="results/headline.png"):
    """Grouped bars of cheap models across rungs, with frontier R0 parity lines.

    Each frontier-tier model's R0 accuracy is drawn as a horizontal reference line
    spanning the axes — the bar that cheap models are trying to clear as they climb
    the rungs. Returns the output path.
    """
    acc = accuracy_by_rung(df)
    cheap, frontier = _split_models(df)

    # Rungs in natural (sorted) order; only those any cheap model actually ran.
    rungs = sorted(acc.columns)
    cheap_acc = acc.reindex(index=cheap, columns=rungs)

    fig, ax = plt.subplots(figsize=(9, 5.5))

    # Grouped bars: x = rung group, one bar per cheap model within the group.
    n_models = max(len(cheap), 1)
    total_width = 0.8
    bar_width = total_width / n_models
    x = range(len(rungs))
    cmap = plt.get_cmap("tab10")
    for i, model in enumerate(cheap):
        offsets = [xi - total_width / 2 + bar_width * (i + 0.5) for xi in x]
        heights = [
            0.0 if pd.isna(v) else float(v) for v in cheap_acc.loc[model, rungs].tolist()
        ]
        ax.bar(offsets, heights, width=bar_width, label=model, color=cmap(i % 10))

    # Frontier R0 scores as horizontal reference lines (the parity bars).
    line_styles = ["--", "-.", ":", (0, (3, 1, 1, 1))]
    for j, model in enumerate(frontier):
        if "R0_baseline" in acc.columns:
            score = acc.loc[model, "R0_baseline"]
        else:
            # Fall back to whatever baseline rung exists for this model.
            r0_cols = [c for c in acc.columns if c.startswith("R0")]
            score = acc.loc[model, r0_cols[0]] if r0_cols else float("nan")
        if pd.isna(score):
            continue
        ax.axhline(
            y=float(score),
            linestyle=line_styles[j % len(line_styles)],
            color="black",
            linewidth=1.5,
            alpha=0.8,
            label=f"{model} (R0 parity = {float(score):.0%})",
        )

    ax.set_xticks(list(x))
    ax.set_xticklabels(rungs, rotation=0)
    ax.set_ylim(0, 1.05)
    ax.set_ylabel("Execution accuracy")
    ax.set_xlabel("Rung")
    ax.set_title("Cheap models across rungs vs. frontier R0 parity")
    ax.legend(loc="lower right", fontsize=8, framealpha=0.9)
    ax.grid(axis="y", linestyle=":", alpha=0.4)

    return _save(fig, out)


def plot_cost(df: pd.DataFrame, out="results/cost.png"):
    """Bar chart of USD cost per correct answer, per model. Returns the output path."""
    cpc = cost_per_correct(df).sort_values("cost_per_correct")
    fig, ax = plt.subplots(figsize=(8, 5))

    models = list(cpc.index)
    values = [0.0 if pd.isna(v) else float(v) for v in cpc["cost_per_correct"].tolist()]
    cmap = plt.get_cmap("tab10")
    bars = ax.bar(models, values, color=[cmap(i % 10) for i in range(len(models))])

    # Annotate "no correct answers" bars so a zero-height bar is not mistaken for free.
    for bar, (_, row) in zip(bars, cpc.iterrows()):
        if pd.isna(row["cost_per_correct"]):
            ax.text(
                bar.get_x() + bar.get_width() / 2,
                0,
                "n/a",
                ha="center",
                va="bottom",
                fontsize=8,
            )

    ax.set_ylabel("USD cost per correct answer")
    ax.set_xlabel("Model")
    ax.set_title("Cost efficiency: USD per correct answer")
    ax.grid(axis="y", linestyle=":", alpha=0.4)
    plt.setp(ax.get_xticklabels(), rotation=30, ha="right")

    return _save(fig, out)


def _save(fig, out):
    """Save ``fig`` to ``out`` (creating parent dirs) and close it. Returns the path."""
    out_path = Path(out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)
    return out_path
