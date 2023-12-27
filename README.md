# Flexexecutor

![Testing](https://github.com/leavers/flexexecutor/workflows/Test%20Suite/badge.svg?event=push&branch=main)

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
- `ProcessPoolExecutor` - flexexecutor directly imports the built-in implementation as it
  already has scaling capabilities.

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

`ProcessPoolExecutor` in flexexecutor is just the same as the built-in
`concurrent.futures.ProcessPoolExecutor`, we just import it directly for convenience.
