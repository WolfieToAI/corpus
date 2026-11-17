# Evaluation

Retrieval changes are judged by numbers, not by eyeballing a few answers. `corpus-eval` indexes a
labelled corpus through the production pipeline (queue, worker, real embedding model, real
PostgreSQL) and measures every retrieval strategy on the same questions. CI publishes the report
on every push to `main` (see the *Evaluation* workflow summary).

## Dataset

[`eval/dataset`](../eval/dataset): 8 documents from a fictional robotics company and 47 questions.

The corpus is built to make ranking *hard* rather than trivial:

- **Distractors** - two robot models (R5 / R7) with the same structure and different values, and
  two remote-work policies (France / Germany). Retrieving "a warranty paragraph" is easy;
  retrieving the R7's is the test.
- **Question types** - 25 paraphrases (no shared wording with the answer), 9 exact identifiers
  (`E-317`, `FLT-4012`, endpoints), 6 multi-part questions needing two passages, 3 unanswerable
  questions and 4 conversational follow-ups ("And for the R7?").

**Labels are evidence strings, not chunk ids.** Each question lists short verbatim excerpts of
the passage(s) that answer it; a retrieved chunk is relevant if it contains one. Labels therefore
stay valid when chunking, embeddings or ingestion change - the very things being compared. Unit
tests guarantee every excerpt exists in exactly one document and fits inside a single chunk, so
no label is ambiguous or unreachable.

## Metrics

| Metric | Meaning |
|--------|---------|
| Recall@k | fraction of a question's evidence found in the top k chunks |
| MRR | 1 / rank of the first relevant chunk, averaged |
| nDCG@5 | rank-discounted gain; a chunk only gains if it adds evidence not already retrieved |
| p50 / p95 ms | retrieval latency per question (embedding + searches + re-ranking) |

## Results

Run on GitHub Actions (2 vCPU, no GPU), embeddings
`sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`, cross-encoder
`Xenova/ms-marco-MiniLM-L-6-v2` re-ranking the top 20 fused candidates.
40 answerable standalone questions, 12 chunks.

| Strategy | Recall@1 | Recall@3 | Recall@5 | MRR | nDCG@5 | p50 ms | p95 ms |
|---|---:|---:|---:|---:|---:|---:|---:|
| Full-text only | 0.738 | 0.975 | 1.000 | 0.869 | 0.873 | 7 | 12 |
| Vector only | 0.550 | 0.825 | 0.912 | 0.717 | 0.740 | 17 | 40 |
| Hybrid (RRF) | 0.838 | 1.000 | 1.000 | 0.929 | 0.918 | 19 | 29 |
| **Hybrid + cross-encoder** | **0.900** | 1.000 | 1.000 | **0.958** | **0.945** | 870 | 981 |

MRR by question type:

| Strategy | paraphrase (25) | keyword (9) | multi (6) |
|---|---:|---:|---:|
| Full-text only | 0.810 | 0.944 | 1.000 |
| Vector only | 0.793 | 0.594 | 0.583 |
| Hybrid (RRF) | 0.907 | 0.944 | 1.000 |
| Hybrid + cross-encoder | 0.973 | 1.000 | 0.833 |

### Reading the results

- **Neither search mode wins alone.** Vector search understands paraphrases but misses exact
  identifiers (MRR 0.59 on error codes; `q29 "What is FLT-4012?"` is not in its top 5).
  Full-text nails identifiers but ranks paraphrases worse. **Hybrid fusion beats both on every
  aggregate metric** - the reason the default pipeline is hybrid.
- **The cross-encoder buys precision at the top**: Recall@1 0.84 -> 0.90, MRR 0.93 -> 0.96. That
  matters because the first sources shown to the LLM weigh most on the answer. It is slightly
  worse on multi-part questions (0.83): re-ranking each passage against the whole question
  favours the passage that answers one part best and can push the second part down.
- **Cost**: ~0.85 s of CPU per question on a CI runner, against ~20 ms without it. Hence
  `RERANKER=none` by default and a one-line switch (`RERANKER=cross-encoder`) where answer
  quality outweighs latency, or on a GPU.
- **Limits**: the corpus is small (12 chunks), so Recall@5 saturates; Recall@1, MRR and nDCG
  are the discriminating metrics here. Growing the dataset is on the roadmap.

### What the evaluation caught

The first run reported **full-text hit@5 = 0 on error-code questions**. Cause:
`websearch_to_tsquery` ANDs every word, so "What does error E-317 mean?" required the passage to
also contain *what*, *does* and *mean*. Full-text search had silently contributed nothing to
hybrid retrieval for natural-language questions. Queries are now reduced to content words
(English/French/German function words removed, identifiers kept intact) and OR-ed; the numbers
above are after the fix.

The same workflow exposed a configuration bug: an empty `LLM_API_KEY=` line was read as a
key, enabling LLM features that then failed. Blank secrets are now treated as unset.

## Conversational follow-ups

Follow-up questions are rewritten into standalone search queries from the conversation before
retrieval (generation still sees the original question and history). The evaluation compares
the raw follow-up with the rewritten query; it needs an LLM (`LLM_API_KEY`), so CI reports it only
when the secret is configured. Without rewriting, MRR on follow-ups is 0.71 even though the
relevant chunk is in the top 5 - with only 12 chunks, a vague query still lands nearby; on a
real corpus it would not.

## Answer quality (LLM judge)

`corpus-eval answers` runs the full path - rewrite, retrieve, generate with the LLM - then a
separate LLM call grades each answer with a structured output:

- **faithfulness** - every claim is supported by the retrieved sources;
- **correctness** - agrees with the human reference, or abstains on unanswerable questions;
- **citation validity** - every `[n]` supports the sentence it ends;
- **abstention accuracy** - on the 3 questions the documents cannot answer.

It calls the LLM API (about three requests per question), so it runs on demand: locally, or
from the *Evaluation* workflow with the `answers` input an `LLM_API_KEY` secret and an `LLM_MODEL` variable.

## Running it

```bash
docker compose up -d db
pip install -e ".[local-embeddings]"
RERANKER=cross-encoder corpus-eval retrieval      # writes eval/results/retrieval.{md,json}
LLM_API_KEY=... LLM_MODEL=... corpus-eval answers --limit 10
```
