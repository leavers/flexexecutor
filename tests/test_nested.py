import itertools
import operator
import os
import time
from typing import Any, cast

import pytest

import flexexecutor

InterpreterPoolExecutor = cast(
    Any, getattr(flexexecutor, "InterpreterPoolExecutor", None)
)
_HAS_INTERPRETER = InterpreterPoolExecutor is not None
_KINDS = "TPI"
_PATHS = [
    "".join(chars)
    for depth in range(1, 4)
    for chars in itertools.product(_KINDS, repeat=depth)
]

_XFAIL_REASON = (
    "ProcessPoolExecutor starts a daemon queue feeder thread; subinterpreters "
    "do not allow daemon threads"
)


def _i_before_p(path: str) -> bool:
    first_i = path.find("I")
    return first_i != -1 and "P" in path[first_i + 1 :]


def _make_executor(kind: str):
    if kind == "T":
        return flexexecutor.ThreadPoolExecutor(max_workers=1)
    if kind == "P":
        return flexexecutor.ProcessPoolExecutor(max_workers=1)
    return InterpreterPoolExecutor(max_workers=1)


def _assert_clean(executor) -> None:
    if isinstance(executor, flexexecutor.ProcessPoolExecutor):
        assert not executor._processes
    else:
        assert not any(thread.is_alive() for thread in executor._threads)


def _run_nested(path: str, index: int = 0):
    if index == len(path):
        return []

    kind = path[index]
    executor = _make_executor(kind)
    with executor:
        nested = executor.submit(_run_nested, path, index + 1).result(timeout=20)
    _assert_clean(executor)
    return [kind, *nested]


def _process_max_tasks_leaf():
    with flexexecutor.ProcessPoolExecutor(
        max_workers=1,
        max_tasks_per_child=1,
        idle_timeout=None,
    ) as executor:
        first = executor.submit(os.getpid).result(timeout=20)
        second = executor.submit(os.getpid).result(timeout=20)
    _assert_clean(executor)
    return first, second


def _interpreter_idle_leaf():
    with InterpreterPoolExecutor(max_workers=1, idle_timeout=0.2) as executor:
        assert executor.submit(operator.add, 1, 2).result(timeout=20) == 3
        deadline = time.monotonic() + 5
        while any(thread.is_alive() for thread in executor._threads):
            if time.monotonic() >= deadline:
                break
            time.sleep(0.05)
        assert not any(thread.is_alive() for thread in executor._threads)
        assert executor.submit(operator.mul, 3, 4).result(timeout=20) == 12
    _assert_clean(executor)
    return "ok"


_PARAMS = [
    pytest.param(
        path,
        marks=(
            pytest.mark.xfail(
                strict=True,
                raises=flexexecutor.BrokenProcessPool,
                reason=_XFAIL_REASON,
            )
            if _HAS_INTERPRETER and _i_before_p(path)
            else ()
        ),
    )
    for path in _PATHS
]


@pytest.mark.timeout(30)
@pytest.mark.parametrize("path", _PARAMS, ids=lambda path: path)
def test_nested_executor(path: str):
    if not _HAS_INTERPRETER and "I" in path:
        pytest.skip("InterpreterPoolExecutor requires Python 3.14+")
    assert _run_nested(path) == list(path)


def test_thread_then_process_max_tasks():
    with flexexecutor.ThreadPoolExecutor(max_workers=1) as outer:
        first, second = outer.submit(_process_max_tasks_leaf).result(timeout=20)
    _assert_clean(outer)
    assert first != second


@pytest.mark.skipif(not _HAS_INTERPRETER, reason="3.14+ only")
def test_process_then_interpreter_idle():
    with flexexecutor.ProcessPoolExecutor(max_workers=1) as outer:
        assert outer.submit(_interpreter_idle_leaf).result(timeout=20) == "ok"
    _assert_clean(outer)
