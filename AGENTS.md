# Project guidance

- Use Python 3.10+ and the course PDFs in docs/ as the API contract, especially
  Student Development Guide Section 6.2 and Appendix B.
- Keep changes small and reviewable. Existing commands remain read-only/dry-run.
  Only trade_once.py --submit may enable role-scoped proposal/review POSTs.
  Require journal protection, independent Guardian checks, and history reconciliation.
  Do not execute live writes as development verification; use mocked lifecycle tests.
- Prefer plain functions, descriptive names, and simple persistent JSON state.
  Reuse pure calculation helpers, but each agent must fetch its own evidence.
  Keep README focused on current commands rather than accumulating milestone history.
- Keep role credentials separate and in ignored local JSON configuration. Never
  log keys, request headers, raw responses, or send credentials to an LLM.
- Use mocked HTTP responses for tests; never use live writes as verification.
- Run `python -m unittest discover -s tests -v` and `git diff --check`.
- Document scaffold adaptations and AI assistance for the course report.
