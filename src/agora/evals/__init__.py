"""Agent evaluation harness.

Subpackages mirror ``agora.agents``: each agent that benefits from a
labeled benchmark gets its own module here. Today only ``routing`` is
populated.

Eval modules are deliberately separate from ``tests/`` because:

1. They run against committed scenario fixtures (``evals/<agent>/*.json``)
   and emit a baseline report — they are *not* unit tests.
2. They will, in future PRs, fan out to real LLM providers; bundling
   them under ``pytest`` would make CI slow + costly + flaky. Eval
   harnesses are invoked via ``make eval-<agent>`` instead.
3. Pytest is configured with ``testpaths = ["tests"]`` so ``evals/``
   stays uncollected even if scenario JSON happens to live alongside.

The harness module itself is in-package (``src/agora.evals.routing``)
so it is type-checked by mypy and bandit-scanned. Scenario data and
baseline reports live at top-level ``evals/<agent>/`` to keep code/data
boundaries clean.
"""
