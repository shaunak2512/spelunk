# Wave 1 · report  (NEW file + NEW tests)

- **Worktree:** `../spelunk-wt/report`  **Branch:** `wave1/report`
- **Create:** `spelunk/eval/report.py` **and** `tests/test_report.py` (write the tests **first**).

Read `_SETUP.md` first. Turn `results.csv` (rows = `spelunk.eval.schemas.RunResult`; columns =
`RESULTS_CSV_COLUMNS`) into the headline result. Use **pandas + matplotlib**.

## Functions
- `load_results(csv_path) -> pd.DataFrame`
- `accuracy_by_rung(df) -> pd.DataFrame`  (index = model, columns = rung, values = mean `ex_correct`)
- `cost_per_correct(df) -> pd.DataFrame`   (summed `usd_cost` / #correct, by model)
- `plot_headline(df, out="results/headline.png")`  — grouped bars: cheap models across rungs, with
  frontier-tier R0 scores drawn as horizontal **reference lines** (the parity bar).
- `plot_cost(df, out="results/cost.png")`

## Tests
Build a synthetic CSV/DataFrame of ~8 `RunResult` rows (2 cheap models × 3 rungs + 2 frontier @ R0).
Assert `accuracy_by_rung` shape/values and `cost_per_correct` math; assert the plot functions write PNGs
(into `tmp_path`). Use the headless backend: `matplotlib.use("Agg")`.

## Done when
`uv run pytest tests/test_report.py` is green. Commit to `wave1/report`.
