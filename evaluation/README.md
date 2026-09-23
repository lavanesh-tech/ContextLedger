# Evaluation

Retrieval-quality evaluation for hybrid temporal RAG, built in **Phase 18**.

Planned contents:

- a labelled evaluation dataset of questions paired with the fact versions that
  *should* be retrieved, including questions pinned to a point in time
  ("what was valid at T?") and questions that must be refused across tenants;
- runners that report Recall@K, Precision@K, MRR, temporal correctness %, and
  authorization correctness %, writing results in the same JSON format as
  `benchmarks/results/`.

Any synthetic data in this folder is labelled **synthetic evaluation dataset**.
