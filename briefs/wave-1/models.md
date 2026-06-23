# Wave 1 · models  (NEW file + NEW tests)

- **Worktree:** `../spelunk-wt/models`  **Branch:** `wave1/models`
- **Create:** `spelunk/agent/models.py` **and** `tests/test_models.py` (write the tests **first**).

Read `_SETUP.md` first. Vendor-agnostic model loading + cost math from `configs/models.yaml`.
**No hardcoded model ids/prices in code** — read them from the yaml.

## Functions
- `ModelSpec` (pydantic: `name, provider, model_id, tier, price_in, price_out`)
- `load_models_config(path="configs/models.yaml") -> dict[str, ModelSpec]`
- `load_model(name) -> BaseChatModel`  via `langchain.chat_models.init_chat_model(f"{provider}:{model_id}")`
- `usd_cost(name, prompt_tokens, completion_tokens) -> float`
  ( = `prompt_tokens/1e6 * price_in + completion_tokens/1e6 * price_out` )

## Tests (NO network / NO real API calls)
- Point `load_models_config` at a tiny synthetic yaml in `tmp_path`; assert it parses 2–3 specs with prices.
- Assert `usd_cost` math on known token counts.
- For `load_model`: do **not** invoke the model. Either assert it returns an object when keys are set, or
  `pytest.mark.skipif` when env keys are absent. The suite must stay green **offline**.

## Done when
`uv run pytest tests/test_models.py` is green offline. Commit to `wave1/models`.
