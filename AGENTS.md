# Project guidance

- Use Python 3.10+ and the course PDFs in docs/ as the API contract, especially
  Student Development Guide Section 6.2 and Appendix B.
- Keep changes small and reviewable. This milestone is read-only: only portfolio,
  policies, and market prices GET requests. Do not add proposals or reviews yet.
- Keep role credentials separate and in ignored local JSON configuration. Never
  log keys, request headers, raw responses, or send credentials to an LLM.
- Use mocked HTTP responses for tests; never use live writes as verification.
- Run `python -m unittest discover -s tests -v` and `git diff --check`.
- Document scaffold adaptations and AI assistance for the course report.
