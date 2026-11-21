# Flexexecutor

[![Testing](https://github.com/leavers/flexexecutor/workflows/Test%20Suite/badge.svg)](https://github.com/leavers/flexexecutor/actions)
[![Package version](https://img.shields.io/pypi/v/flexexecutor.svg)](https://pypi.org/project/flexexecutor/)
[![Python](https://img.shields.io/pypi/pyversions/flexexecutor.svg)](https://pypi.org/project/flexexecutor/)

Flexexecutor provides executors that can automatically scale the number of workers up
and down.

## Overview

Flexexecutor implements several subclasses of `concurrent.futures.Executor`.
Like the built-in `ThreadPoolExecutor` and `ProcessPoolExecutor`,
they create multiple workers to execute tasks concurrently.
Additionally, it can shut down idle workers to save resources. These subclasses are:

- `ThreadPoolExecutor` - thread concurrency, same name as the built-in class and can
  be a in-place replacement.
- `AsyncPoolExecutor` - coroutine concurrency. Note that in flexexecutor's
  implementation, all coroutines are executed in a dedicated worker thread.
- `ProcessPoolExecutor` - process concurrency with idle-worker shutdown and
  `max_tasks_per_child` recycling, API-compatible with the built-in class.
- `InterpreterPoolExecutor` - subinterpreter concurrency on Python 3.14+.

## Features

- Supports various concurrency modes: threads, processes and coroutines.
- Automatically shut down idle workers to save resources.
- Single file design, keeps the code clean and easy for hackers to directly take away
  and add more features.

## Installation

Flexexecutor is available on [PyPI](https://pypi.org/project/flexexecutor/):

```shell
pip install flexexecutor
```

## Usage

### ThreadPoolExecutor

```python
from flexexecutor import ThreadPoolExecutor


def task(i):
    import time

    print(f"task {i} started")
    time.sleep(1)

if __name__ == "__main__":
    with ThreadPoolExecutor(
        # 1024 is the default value of max_workers, since workers are closed if they are
        # idle for some time, you can set it to a big value to get better short-term
        # performance.
        max_workers=1024,
        # Timeout for idle workers.
        idle_timeout=60.0,
        # These parameters are given for compatibility with the built-in
        # `ThreadPoolExecutor`, I don't use them very often, do you?
        thread_name_prefix="Task",
        initializer=None,
        initargs=(),
    ) as executor:
        for i in range(1024):
            executor.submit(task, i)
```

### AsyncPoolExecutor

`AsyncPoolExecutor` runs all coroutines in a dedicated worker thread with its own
event loop. Note that **tasks are not executed in the thread where they are
submitted** — they are sent to the internal worker thread for execution.

```python
from flexexecutor import AsyncPoolExecutor


async def task(i):
    import asyncio

    print(f"task {i} started")
    await asyncio.sleep(1)

if __name__ == "__main__":
    # AsyncPoolExecutor behaves just like ThreadPoolExecutor except it only accepts
    # coroutine functions.
    with AsyncPoolExecutor(
        # Default value of max_workers is huge, if you don't like it, set it smaller.
        max_workers=1024,
        # Idle timeout for the working thread.
        idle_timeout=60.0,
        # These parameters are given for compatibility with the built-in
        # `ThreadPoolExecutor`, I don't use them very often, do you?
        thread_name_prefix="Task",
        initializer=None,
        initargs=(),
    ) as executor:
        for i in range(1024):
            executor.submit(task, i)
```

### ProcessPoolExecutor

Flexexecutor implements its own `ProcessPoolExecutor`, derived from CPython
3.15. It keeps the standard API and adds idle-worker shutdown.

```python
from flexexecutor import ProcessPoolExecutor

if __name__ == "__main__":
    with ProcessPoolExecutor(
        # Maximum number of worker processes.
        max_workers=4,
        # Exit a worker after it has been idle for this many seconds.
        # None or a negative value disables idle shutdown.
        idle_timeout=60.0,
        # Replace a worker after it has completed this many tasks.
        # None means workers live until the executor is shut down.
        max_tasks_per_child=None,
    ) as executor:
        print(executor.submit(pow, 2, 10).result())
```

`idle_timeout` defaults to 60 seconds. To replace workers because of the
workload rather than idleness, use `max_tasks_per_child`; the native default is
`None`, which keeps workers alive for the lifetime of the executor.

When `idle_timeout` is active and `mp_context` is not supplied, flexexecutor
selects a non-`fork` start method so that the manager thread can safely start a
replacement worker: `forkserver` on Unix outside macOS, `spawn` on Windows and
macOS. On Python 3.11-3.13 this differs from the standard library's default
`fork` on Linux. Passing an explicit `fork` context together with an active
`idle_timeout` raises `ValueError`; pass `idle_timeout=None` if you need `fork`.
`initializer` and `initargs` must therefore be pickleable.

Workers are reused between tasks. Module-level state, globals and initializer
state changed by one task stay in that worker for the next task; only task
arguments and results are pickled. Set `max_tasks_per_child=1` to get a fresh
process for every task.

On Python 3.14+, the standard `map(buffersize=...)`, `terminate_workers()` and
`kill_workers()` APIs are also available.

### InterpreterPoolExecutor

`InterpreterPoolExecutor` is available on Python 3.14+ and runs each worker in a
subinterpreter, giving every worker its own GIL. It is useful for CPU-bound
Python code without process startup or separate address spaces.

```python
from flexexecutor import InterpreterPoolExecutor

if __name__ == "__main__":
    with InterpreterPoolExecutor(
        max_workers=4,
        idle_timeout=60.0,
        max_tasks_per_child=None,
    ) as executor:
        print(executor.submit(pow, 2, 10).result())
```

Tasks, arguments and return values are pickled across the interpreter
boundary. Each interpreter has isolated modules and globals; an initializer runs
once per worker interpreter. `max_tasks_per_child` and `idle_timeout` work the
same way as in the process executor. C extensions must support subinterpreters;
extensions that do not may fail or leak. Interpreters share the parent process
address space, so a crash still crashes the process.
