# Contributing to Herder

Thanks for your interest in contributing! This guide covers dev setup, discipline, and how to add features.

## Development Setup

```bash
git clone https://github.com/cleonhp88/herder.git
cd herder
uv sync
uv run pytest  # should pass all 285 tests
```

Requires:
- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- macOS (Linux support experimental)

## Discipline

**TDD is mandatory.** All code changes must:

1. Write a test first (test file in `tests/`)
2. Run the test — it fails (RED)
3. Write minimal code to pass (GREEN)
4. Refactor if needed (IMPROVE)
5. Run full test suite: `uv run pytest`
   - All tests must pass
   - 0 warnings
   - Coverage ≥80% for new code

**Before opening a PR:**
```bash
uv run pytest -v              # All pass?
uv run pytest --cov=herder    # Coverage OK?
```

## Code Layout

```
herder/
├── cli.py          # Entry point; thin command parsing
├── config.py       # Config loading & validation
├── services/       # Business logic
│   ├── enqueue.py
│   ├── worker.py
│   ├── store.py
│   └── ...
├── db/
│   ├── schema.py
│   ├── migrations.py
│   └── models.py
├── providers/      # Provider adapters
│   ├── base.py
│   ├── cli.py
│   └── ollama_http.py
└── sandbox/        # Seatbelt + security
    └── mac.py
tests/
├── conftest.py     # Pytest fixtures
└── test_*.py       # Test files
```

## Adding a Provider

1. Implement the provider in `herder/providers/` (inherit from `ProviderBase`):
   ```python
   from herder.providers.base import ProviderBase
   
   class MyProvider(ProviderBase):
       def invoke(self, prompt: str, **kwargs) -> dict:
           # Returns {status, output, tokens_in, tokens_out, error, ...}
           ...
   ```

2. Register it in `herder/providers/__init__.py` or `herder/registry.py`

3. Add config support in `herder/config.py` (parsing the provider block)

4. Write tests in `tests/test_my_provider.py`

5. Document in [docs/architecture.md](docs/architecture.md#how-to-add-a-provider)

6. Run full test suite and submit PR

## Testing

- Unit tests: `tests/test_*.py`
- Integration tests: `tests/test_*_integration.py`
- E2E tests: `tests/test_*_e2e.py`

Run specific test:
```bash
uv run pytest tests/test_config.py -v
```

Run with coverage:
```bash
uv run pytest --cov=herder --cov-report=term-missing
```

## PR Guidelines

- **Small PRs.** Keep changes focused (ideally <10 files touched).
- **Clear commit messages.** Explain the "why", not just "what".
- **Test coverage.** New code must have tests.
- **No breaking changes** to public APIs without discussion.

## Questions?

Open an issue or reach out on GitHub. See [docs/architecture.md](docs/architecture.md) for design details.

---

Happy contributing!
