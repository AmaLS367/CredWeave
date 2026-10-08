# 🤝 Contributing to CredWeave

Thank you for your interest in contributing to **CredWeave**! 🎉 

CredWeave is a provider-agnostic, protocol-independent credential lifecycle engine designed for high reliability, strict security invariants, and zero runtime dependencies. We welcome contributions, bug reports, feature proposals, and documentation improvements.

---

## 🧭 Table of Contents

- [Code of Conduct & Philosophy](#-code-of-conduct--philosophy)
- [Development Setup](#-development-setup)
- [Running Quality Checks](#-running-quality-checks)
- [Architectural Invariants](#-architectural-invariants)
- [Security Guidelines](#-security-guidelines)
- [Pull Request Process](#-pull-request-process)
- [Releases](#-releases)

---

## 💡 Code of Conduct & Philosophy

When contributing to CredWeave, please keep our core tenets in mind:

1. **Protocol & Network Agnosticism:** CredWeave manages credentials and their state transitions. It does *not* make HTTP or network requests itself.
2. **Zero Runtime Dependencies:** The core library strictly relies on the Python standard library. External runtime dependencies will generally not be accepted for the core package.
3. **Respectful Collaboration:** Be welcoming, constructive, and considerate of others.

---

## 🛠️ Development Setup

### 1. Prerequisites
- **Python 3.10+** (tested on 3.10, 3.11, 3.12, 3.13)
- `git`
- `pip` and standard `venv` (or your preferred tool like `uv`, `poetry`, etc.)

### 2. Local Environment Setup

1. **Fork and clone the repository:**
   ```bash
   git clone https://github.com/AmaLS367/CredWeave.git
   cd CredWeave
   ```

2. **Create and activate a virtual environment:**
   ```bash
   python -m venv .venv
   
   # Windows (PowerShell):
   .venv\Scripts\Activate.ps1
   
   # Linux / macOS:
   source .venv/bin/activate
   ```

3. **Install CredWeave in editable mode with development dependencies:**
   ```bash
   pip install -e ".[dev]"
   ```

---

## 🧪 Running Quality Checks

CredWeave maintains strict code quality, 100% type coverage, and zero lint regressions. All tests and checks must pass before merging.

### 1. Run Unit & Integration Tests
```bash
pytest
```
To run tests with branch coverage reporting:
```bash
pytest --cov=credweave --cov-report=term-missing
```

### 2. Linting & Formatting (Ruff)
Check for lint errors and formatting violations:
```bash
ruff check .
ruff format --check .
```
Automatically fix linting and formatting issues where possible:
```bash
ruff check --fix .
ruff format .
```

### 3. Static Type Checking (Mypy)
We enforce `strict = true` across the entire codebase:
```bash
mypy src
```

---

## 📐 Architectural Invariants

CredWeave follows **Clean Architecture** principles. Please review our [Architecture Guide](architecture/architecture.md) before designing new features:

- **Inward Dependency Rule:**
  - `credweave.domain` cannot import anything outside the Python standard library, nor from `application` or `infrastructure`.
  - `credweave.application` defines interfaces/protocols (`ports/`) and orchestration services (`services/`).
  - `credweave.infrastructure` contains swappable technology adapters (clocks, storage backends, secret loaders).
- **Public API Hygiene:**
  - All public classes, types, and functions must be explicitly exported in `src/credweave/__init__.py` and listed in `__all__`.
  - Internal helper modules must be placed in `credweave._internal` and kept private.

---

## 🛡️ Security Guidelines

Secrets handling demands uncompromising security guarantees:

1. **Automatic Secret Masking:** Secret values must never appear in plaintext inside `__repr__`, `__str__`, logs, or formatted error messages. Always ensure secrets are redacted with `'***'`.
2. **Safe Secret Retrieval:** Secrets should only be accessible through explicit accessors such as `lease.credential.get_secret("key")`.
3. **No Secret Leaks in Exceptions:** Custom exceptions must never format or interpolate raw secret values into error messages.
4. **Immutability:** Internal credential mappings, secrets, and metadata views must remain immutable (`MappingProxyType` or frozen structures).

---

## 🚀 Pull Request Process

1. **Create a branch:**
   ```bash
   git checkout -b feature/your-feature-name
   ```
2. **Write tests:**
   Add comprehensive unit tests in `tests/unit/` or integration tests in `tests/integration/` covering your changes.
3. **Run all verification checks locally:**
   ```bash
   ruff check .
   ruff format --check .
   mypy src
   pytest
   ```
4. **Commit with clear messages:**
   Follow conventional commit style (e.g. `feat: add RoundRobinStrategy`, `fix: mask secret in lease error message`).
5. **Open a Pull Request:**
   Provide a clear summary of your changes, reference any related issues, and ensure CI passes.

## 📦 Releases

Maintainers publish releases to PyPI through the `Release` workflow, which runs only when a GitHub Release is published. See [RELEASING.md](RELEASING.md) for the Trusted Publisher setup and the release procedure.
