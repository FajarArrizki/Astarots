# Dependencies — Astarots

Current tooling and runtime dependencies. This is kept minimal by design — dependencies are added only when justified.

---

## Runtime

| # | Name | Version | Description | Link |
|---|---|---|---|---|
| 1 | Python | 3.14.6 | Language runtime (`requires-python >=3.12`) | [python.org](https://www.python.org/) |

## Tooling

| # | Name | Version | Description | Link |
|---|---|---|---|---|
| 1 | uv | 0.11.32 | Fast Python package & project manager | [docs.astral.sh/uv](https://docs.astral.sh/uv/) |

## Application Dependencies

| # | Name | Version | Description | Link |
|---|---|---|---|---|
| — | *none yet* | — | — | — |

---

## Notes

- **Package manager**: `uv` handles virtualenvs, dependency resolution, and lockfiles (`uv.lock`).
- **Adding a dep**: `uv add <package>` — updates both `pyproject.toml` and `uv.lock`.
- **This table should be updated** whenever a new dependency is added. Keep the `#` column sequential.
