# Contributing

Thank you for considering a contribution to `ai-browser`.

**Whether you are a human or an AI agent, the full technical guidelines are in [`AGENTS.md`](AGENTS.md).** This page is only a quick entry point.

## Quick start

```bash
git clone <this-repo>
cd ai-browser
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/pip install -e '.[dev]'
./venv/bin/python -m pytest tests/unit -v
```

## Submitting a change

1. For anything non-trivial, open an issue describing the problem first. Small fixes can go straight to a PR.
2. Branch from `main`. Keep each PR to one change.
3. Run the unit tests. If the change affects service behavior, also run the e2e smoke tests.
4. Fill in the PR description as described in section 6 of `AGENTS.md`.

## Scope

This is a general-purpose browser automation service. Accepted contributions include:

- New browser operations and new entry points (HTTP / MCP / CLI)
- Improvements to engine stability, performance, and block detection
- Tests, documentation, and deployment scripts

Not accepted: sharing or bulk-managing login credentials, bypassing paywalls or access controls, and accessing accounts or systems without authorization. See section 2 of `AGENTS.md`.

## Security issues

If you find a security vulnerability (for example, the service being reachable by unauthorized local processes or over the network), do not open a public issue. Report it privately through GitHub's private security advisory feature, or contact the maintainer directly.

## License

Contributions are released under this repository's MIT license.
