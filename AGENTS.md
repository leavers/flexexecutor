# Flexexecutor - AI Agent Guide

## Project Overview

Flexexecutor is a Python library that provides `concurrent.futures.Executor` subclasses with automatic worker scaling capabilities. Unlike the standard library's `ThreadPoolExecutor` and `ProcessPoolExecutor`, flexexecutor can shut down idle workers to save resources.

**Key Design Philosophy**: Single-file design (`flexexecutor.py`) - keeps the code clean and easy for hackers to directly take away and customize.

### Main Components

1. **ThreadPoolExecutor** - Thread-based concurrency with idle timeout support. Compatible with Python's built-in `concurrent.futures.ThreadPoolExecutor` as a drop-in replacement.

2. **AsyncPoolExecutor** - Coroutine-based concurrency. All coroutines run in a dedicated worker thread with its own event loop.

3. **ProcessPoolExecutor** - Directly imported from `concurrent.futures` (built-in already has scaling).

4. **WorkerContext / AsyncWorkerContext** - Classes for customizing worker initialization, task execution, and cleanup.

## Technology Stack

- **Language**: Python 3.11+
- **Package Manager**: [PDM](https://pdm.fming.dev/) (Python Dependency Manager)
- **Build Backend**: `pdm-backend`
- **Task Runner**: [Nox](https://nox.thea.codes/)
- **Testing**: pytest, pytest-asyncio, pytest-cov, pytest-mock, pytest-timeout
- **Linting**: ruff, autoflake, ty (type checker)
- **CI/CD**: GitHub Actions

## Project Structure

```
flexexecutor/
├── flexexecutor.py      # Main module (single file, ~811 lines)
├── pyproject.toml       # Project configuration (PDM, tools config)
├── noxfile.py          # Task automation (format, test, lint)
├── pdm.lock            # Dependency lock file
├── tests/              # Test suite
│   ├── conftest.py     # Test utilities (alive_threads, wait_for_alive_threads)
│   ├── test_thread.py  # ThreadPoolExecutor tests
│   └── test_async.py   # AsyncPoolExecutor tests
├── .github/workflows/  # CI/CD
│   ├── test-suite.yml  # Run tests on push/PR
│   └── publish.yml     # Publish to PyPI on tag
└── README.md           # User documentation
```

## Build and Development Commands

### Setup

```bash
# Install PDM (if not already installed)
pip install pdm

# Install dependencies
pdm install

# Install dev dependencies
pdm install --dev
```

### Running Tests

```bash
# Run tests (uses nox)
pdm run nox -s test

# Run tests for CI (installs deps automatically)
pdm run nox -s test_for_ci

# Run tests on all supported Python versions (3.11, 3.12, 3.13)
pdm run nox -s test_all

# Run tests directly with pytest
pytest --cov flexexecutor.py --cov-report term-missing tests
```

### Code Formatting

```bash
# Format code (autoflake + ruff)
pdm run nox -s format

# Check formatting without modifying files
pdm run nox -s format_check
```

### Type Checking

```bash
# Run ty type checker
pdm run nox -s ty
```

### Cleaning

```bash
# Clean build artifacts
pdm run nox -s clean
```

## Code Style Guidelines

### Line Length
- Maximum 88 characters (configured in `pyproject.toml` under `[tool.ruff]`)

### Import Style
- Use `autoflake` to remove unused imports automatically
- Group imports: stdlib first, then third-party, then local
- Use `from typing import TYPE_CHECKING` for type-only imports

### Type Hints
- Type hints are encouraged but use `TYPE_CHECKING` block for imports needed only for typing
- Example pattern from the code:
  ```python
  if TYPE_CHECKING:
      from typing_extensions import Callable, ParamSpec, TypeVar
      P = ParamSpec("P")
      T = TypeVar("T")
  ```

### Naming Conventions
- Class names: `PascalCase` (e.g., `ThreadPoolExecutor`, `WorkerContext`)
- Private/internal: prefix with underscore (e.g., `_WorkItem`, `_worker`)
- Constants: `UPPER_CASE` with underscores

### Documentation
- Use docstrings for classes and public methods
- Follow Google-style or similar docstring format
- Include type information in docstrings where helpful

## Testing Strategy

### Test Organization
- `tests/test_thread.py` - Tests for `ThreadPoolExecutor`
- `tests/test_async.py` - Tests for `AsyncPoolExecutor`
- `tests/conftest.py` - Shared test utilities

### Test Patterns
- Use `pytest-mock` for mocking (MockerFixture)
- Use `pytest-asyncio` for async tests
- Use context managers (`with` statement) for executor lifecycle
- Common test utilities:
  - `alive_threads(executor)` - Get list of alive worker threads
  - `wait_for_alive_threads(executor, expect, timeout)` - Wait for expected thread count

### Coverage
- Target: Branch coverage enabled
- Reports: terminal, HTML (`coverage_html_report/`), XML (`.nox/coverage.xml`)
- Configuration in `pyproject.toml` under `[tool.coverage.*]`

### Key Test Scenarios
- Basic task execution
- Multiple tasks on same/multiple threads
- Worker idle timeout behavior
- Initializer success/failure
- Broken pool handling
- Shutdown behavior (wait, cancel_futures)
- atexit handler
- Executor deletion handling

## CI/CD Pipeline

### Test Suite Workflow (`.github/workflows/test-suite.yml`)
- Triggers: push to main, pull requests, manual dispatch
- Matrix: Python 3.11, 3.12, 3.13, 3.14 on Windows, Ubuntu, macOS
- Steps:
  1. Checkout code
  2. Setup Python
  3. Setup PDM
  4. Install nox
  5. Run `pdm run nox -s test_for_ci`

### Publish Workflow (`.github/workflows/publish.yml`)
- Triggers: git tag push (any tag pattern)
- Environment: `release` (for trusted publishing)
- Steps:
  1. Publish to PyPI using `pdm publish`
  2. Create GitHub release with distribution files
- Uses PyPI trusted publishing (OIDC)

## Version Management

- Version is defined in `flexexecutor.py` as `__version__`
- PDM reads version from `flexexecutor.py` (configured in `pyproject.toml`)
- Current version: `0.0.12`

## Dependencies

### Runtime Dependencies
- **None** - The library has zero runtime dependencies

### Development Dependencies
See `[dependency-groups.dev]` in `pyproject.toml` for the complete list.

## Security Considerations

1. **No External Runtime Dependencies**: Reduces supply chain attack surface
2. **Trusted Publishing**: CI uses PyPI trusted publishing (OIDC) instead of API tokens
3. **Thread Safety**: The code uses locks (`Lock`, `Semaphore`) for thread-safe operations
4. **Resource Cleanup**: Proper cleanup of threads and event loops on shutdown
5. **Weak References**: Uses `WeakKeyDictionary` and `WeakSet` to avoid reference cycles

## Common Development Tasks

### Adding a New Feature

1. Implement in `flexexecutor.py`
2. Add tests in `tests/test_thread.py` or `tests/test_async.py`
3. Run `pdm run nox -s format` to format code
4. Run `pdm run nox -s ty` to check types
5. Run `pdm run nox -s test` to run tests
6. Update `__version__` if needed

### Making a Release

1. Update `__version__` in `flexexecutor.py`
2. Commit and push changes
3. Create and push a git tag:
   ```bash
   git tag v0.0.13
   git push origin v0.0.13
   ```
4. GitHub Actions will automatically publish to PyPI and create a GitHub release

## Important Implementation Notes

1. **Single File Design**: All code lives in `flexexecutor.py`. Keep it self-contained.

2. **Worker Idle Timeout**: 
   - Default is 60 seconds
   - Set to `None` or negative value to disable (workers never terminate)
   - Workers check timeout every 0.1 seconds

3. **AsyncPoolExecutor Architecture**:
   - Runs in a dedicated thread with its own event loop
   - Uses `asyncio.TaskGroup` for managing concurrent coroutines
   - Uses `asyncio.Semaphore` to limit concurrency

4. **Python Exit Handler**: 
   - `_python_exit()` is registered with `atexit`
   - Ensures clean shutdown of all executors on interpreter exit

5. **Compatibility**:
   - Python 3.11+ only
   - Compatible with `asyncio.run_in_executor()`
   - Drop-in replacement for stdlib `ThreadPoolExecutor`
