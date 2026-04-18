# Contributing to projmem

Thanks for thinking about contributing. This project is small and opinionated
— please read through before opening a PR.

## What this project is

projmem is **drift-aware code memory for AI agents**. The core differentiator
is *claim-level note verification* — structured beliefs that can be re-checked
against the code and refuted when the code changes.

We are NOT trying to beat grep on one-shot search. Please keep that framing
in mind when proposing features.

## Development setup

```bash
git clone https://github.com/<org>/projmem.git
cd projmem
pip install -e '.[treesitter,dev]'
pytest -q
```

The tree-sitter extra is optional but strongly recommended — ~85% of the
test suite exercises AST-grounded behavior.

## Before opening a PR

1. **One thing per PR.** A bug fix does not need surrounding cleanup; a
   refactor does not need a new feature tucked in.
2. **Every behavior change needs a test.** Regression tests in `tests/`
   follow the `test_<area>.py` pattern.
3. **Run the full suite locally** — `pytest -q` — and make sure there are
   no new failures or xfails.
4. **No silent drift.** If you add a new refs source / predicate / etc.,
   also add the detection that exposes its blind spots. Our governing
   principle: *MemTrace must not pretend completeness.*

## Code conventions

- Python 3.9+ (no f-string-only features, type hints encouraged).
- Keep modules focused: `claims.py` is for claim verification, `artifacts.py`
  for file classification, etc. Don't bundle unrelated concerns.
- Comments explain *why*, not *what*. Assume the next reader can read code.
- Prefer editing existing files to adding new ones.

## Reporting a bug

Use the issue template. Include:
- `projmem --version` (or git SHA if running from source)
- `projmem doctor` output
- Minimal reproducer — ideally a small fixture under `tests/fixtures/`

## Proposing a feature

Open an issue first. The maintainer will push back hard on feature creep
that dilutes the drift-aware memory story. If your feature reduces blind
spots, improves claim coverage, or makes stale state harder to ignore,
it's likely in scope.

## Security issues

Please do NOT open a public issue. See SECURITY.md.
