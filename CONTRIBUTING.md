# Contributing

Contributions and suggestions are welcome. Keep changes focused, explain the user-visible behavior, and add or update tests for changed behavior.

## Development setup

Install Python 3.11 or newer and [uv](https://docs.astral.sh/uv/), then create the locked environment:

```bash
uv sync --frozen
```

Run the local quality gate before opening a pull request:

```bash
uv run pytest -q
uv run ruff check .
uv run pyright
az bicep build --file infra/main.bicep --stdout >/dev/null
bash -n scripts/*.sh
npx -y markdownlint-cli2@0.20.0 '**/*.md'
uv build
```

Live integration tests require an existing Azure OpenAI deployment and are opt-in. See [Validation](docs/validation.md) for the required environment variables and tenant acceptance checks.

## Pull requests

- Open an issue before a large behavioral or architectural change.
- Do not include credentials, tokens, tenant identifiers, prompts, responses, attachment content, or deployment state.
- Do not commit generated `infra/*.json`, `.local/`, `.env`, app packages, or build output.
- Keep provider-specific code behind the engine protocols in `src/agent_app/engines/base.py`.
- Update public documentation when configuration, deployment, security, or operator behavior changes.
- Confirm that default tests do not call live cloud services.

## Legal

By submitting a contribution, you represent that you have the right to license it under this repository's [MIT License](LICENSE). This repository does not currently require a separate contributor agreement. If that changes, the pull-request checks and this guide must be updated before the requirement is enforced.

This project follows its [Code of Conduct](CODE_OF_CONDUCT.md).
