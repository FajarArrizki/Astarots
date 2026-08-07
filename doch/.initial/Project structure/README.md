# Project Structure — Astarots

Modular architecture organized by **function/domain** rather than by file type. Each directory groups everything a feature needs — logic, utilities, tests — so related code stays close and the codebase scales without turning into a flat file dump.

---

## Directory Layout

```
astarots/
├── main.py                  # Entry point
├── pyproject.toml           # Project metadata & dependencies (uv)
├── README.md
│
├── devil/                   # Application source — one subpackage per function
│   ├── core/                #   Shared kernel: config, logging, base types
│   ├── <function>/          #   Self-contained feature module
│   │   ├── __init__.py
│   │   ├── models.py        #   Domain models / dataclasses
│   │   ├── service.py       #   Business logic
│   │   ├── adapters.py      #   I/O boundaries (DB, API, FS)
│   │   └── utils.py         #   Module-scoped helpers
│   └── ...
│
├── tests/                   # Mirrors devil/ structure
│   ├── conftest.py          # Shared fixtures
│   ├── core/
│   ├── <function>/
│   │   ├── test_models.py
│   │   ├── test_service.py
│   │   └── test_adapters.py
│   └── ...
│
└── doch/                    # Design docs, decisions, references
    └── .initial/            # Bootstrapping docs (not shipped)
```

---

## Conventions

### `devil/` — Source

Every feature lives in its own subpackage under `devil/`. No cross-cutting modules like `models/`, `services/`, or `utils/` at the top level — those grow into junk drawers.

Rules:

- **One function, one subpackage.** If a new feature doesn't fit cleanly into an existing one, create a new subpackage.
- **Shared code goes in `devil/core/`.** Config, base exceptions, logging setup, common types. If it's used by 3+ modules, it belongs in core.
- **`__init__.py` re-exports the public API** of each subpackage. Callers never reach into internal modules.

Example for a `user` function:

```
devil/
├── user/
│   ├── __init__.py    # from devil.user import User, UserService
│   ├── models.py      # User, UserCreate, UserUpdate
│   ├── service.py     # UserService: create, find, update, delete
│   ├── adapters.py    # UserRepository (DB), UserNotifier (email)
│   └── utils.py       # validate_email, hash_password
```

### `tests/` — Tests

Mirrors `devil/` one-to-one. Every test file maps to exactly one source file:

```
devil/user/models.py    →  tests/user/test_models.py
devil/user/service.py   →  tests/user/test_service.py
```

### `doch/` — Documentation

Design decisions, architecture notes, and references. The `.initial/` folder holds bootstrapping docs (this one included) that inform the project setup but aren't part of the shipped documentation.

---

## Why This Way

| Problem | How This Solves It |
|---|---|
| Flat `models/` + `services/` directories grow to hundreds of files | Code is grouped by what it does, not what type it is |
| Adding a feature touches files scattered across the tree | One subpackage = one feature boundary |
| Tests get out of sync with source | Mirror structure makes the mapping obvious |
| Hard to delete or extract a feature | Each subpackage is self-contained — drop the folder, drop the tests |
