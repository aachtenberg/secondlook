## What and why

<!-- What changes, and why. Link the issue or the review finding it answers. -->

## Checklist

- [ ] **Tests:** full suite run (`uv sync --all-extras`, then `uv run pytest -rs`); the skips are listed with their reason
- [ ] **Docs reviewed** so they don't drift from the code: every doc this change touches is updated in this PR. Check
  the README (features, *Learn more*), `docs/*.md` (configuration, models, storage, web, mcp, operations: commands,
  code layout, testing, what's next), `recipes/reference.toml`, `.env.example`, the diagrams in `docs/img/`, and the
  docstrings and tool descriptions the model reads. Say below what was updated, or why nothing needed to be.
- [ ] **Secrets scan** green, in CI and in the pre-commit hook (`git config core.hooksPath .githooks`): no keys,
  tokens or real passwords (the made-up lab ones are listed in `.gitleaks.toml`)
- [ ] **Review:** `/review` requested; findings fixed, or answered with why they don't hold

## Docs

<!-- e.g. "docs/web.md: Scheduling now mentions X" or "no doc change: internal refactor, no behaviour change" -->
