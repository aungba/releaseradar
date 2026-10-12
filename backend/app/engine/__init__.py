"""Analysis engine: turns a ``CompareResult`` into a validated ``ReleaseReviewReport``.

Stages 1–5 and 7 are pure and deterministic; only the LLM client touches the network.
"""
