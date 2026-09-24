# Evaluation

Everything here uses a **synthetic evaluation dataset**. Numbers describe that
dataset only.

## Grounded-answer and prompt evaluation (built)

`datasets/grounded_answers.json` (versioned) has one case per situation:
point-in-time questions, superseded values, questions the facts cannot answer,
another tenant's data, facts above the caller's privacy ceiling, disagreeing
sources, and multi-fact citations. Each case builds its own organization in a
throwaway database (`contextledger_eval`) through the real services, then asks
the question through the real pipeline: retrieval, authorization, the versioned
prompt, the LangChain chain and the citation check.

Code: `backend/app/evaluation/` (`dataset.py`, `metrics.py`, `runner.py`).

| Mode | Command | Model | What it measures |
|---|---|---|---|
| deterministic | `make eval` | scripted (free) | **Pipeline only**: the model received the fact versions it needed (`context_correctness`), received no forbidden value (`forbidden_values_supplied`), and an invented citation is always withheld (`citation_guard_withheld_rate`). Says nothing about answer quality. |
| live | `make eval-live` | OpenAI (**costs money**) | Answer accuracy (overall, per category), temporal correctness, insufficient-evidence accuracy, citation validity and recall, ungrounded rate, structured-output validity, latency p50/p95, tokens, and estimated cost if you pass current prices. |

```bash
make eval
export CONTEXTLEDGER_OPENAI_API_KEY=sk-...     # never commit it
make eval-live LIMIT=18 PROMPTS=grounded-answer-v1,grounded-answer-v2 PRICE_IN=<usd per 1M> PRICE_OUT=<usd per 1M>
```

Live mode refuses to run without `--live` (the Make target passes it) and an
API key; `LIMIT` caps the number of cases. Prices are never assumed: without
`PRICE_IN` / `PRICE_OUT` the cost is reported as `null`.

Results are written to `results/answers-<mode>-<prompt>-<timestamp>-<commit>.json`
with the date, commit SHA, whether the working tree was dirty, dataset name and
version, model, prompt version, configuration, metrics and every case outcome.
Only results that were actually produced are committed; none are edited by hand.

The deterministic half also runs in CI as a regression test
(`tests/integration/test_evaluation_pipeline.py`).

Limits: 18 cases is small, retrieval uses the offline word-overlap embedder,
and substring checks can miss a correct answer phrased differently (thousands
separators are normalized, so "$5,375" matches "5375", but "five thousand" would not). Treat live results as a comparison between prompt
versions on this dataset, not as a general accuracy figure.

## Retrieval-quality evaluation (planned, Phase 18)

Recall@K, Precision@K, MRR, temporal correctness and authorization correctness
of retrieval itself, over a labelled dataset of questions paired with the fact
versions that should be retrieved.
