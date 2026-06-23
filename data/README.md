# Data

`bird/` (gitignored) holds the downloaded **BIRD dev set**: per-database SQLite files + gold SQL +
column-description CSVs. Download from the BIRD benchmark release and unpack here.

## Frozen file formats — do NOT change without a barrier (the eval layer is built against these)

### `questions.jsonl`
One JSON object per line. Schema = `spelunk.eval.schemas.BirdQuestion`.
```json
{"question_id": 12, "db_id": "financial", "question": "How many clients opened an account in 1995?", "evidence": "account opening date is in the `date` column", "gold_sql": "SELECT COUNT(*) FROM account WHERE STRFTIME('%Y', date) = '1995'", "difficulty": "moderate"}
```

### `results.csv`
One row per `(model, rung, question)`. Schema = `spelunk.eval.schemas.RunResult`. Header:
```
run_id,question_id,db_id,difficulty,model,rung,predicted_sql,ex_correct,n_llm_calls,n_tool_calls,prompt_tokens,completion_tokens,usd_cost,latency_s,error
```
