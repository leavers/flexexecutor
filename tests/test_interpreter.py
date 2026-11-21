import operator
import sys
import time

import pytest

pytest.importorskip("concurrent.interpreters")

import flexexecutor  # noqa: E402
from tests.conftest import alive_threads  # noqa: E402

InterpreterPoolExecutor = getattr(flexexecutor, "InterpreterPoolExecutor")


def test_simple_run():
    with InterpreterPoolExecutor(max_workers=1, idle_timeout=0.2) as executor:
        assert executor.submit(operator.add, 1, 2).result() == 3
    assert not alive_threads(executor)


def test_error_is_propagated():
    with InterpreterPoolExecutor(max_workers=1, idle_timeout=0.2) as executor:
        future = executor.submit(operator.truediv, 1, 0)
        with pytest.raises(ZeroDivisionError):
            future.result()


def test_idle_worker_is_replaced():
    with InterpreterPoolExecutor(max_workers=1, idle_timeout=0.2) as executor:
        assert executor.submit(operator.add, 1, 2).result() == 3
        deadline = time.monotonic() + 5
        while alive_threads(executor) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not alive_threads(executor)
        assert executor.submit(operator.mul, 3, 4).result() == 12


def test_max_tasks_per_child():
    with InterpreterPoolExecutor(
        max_workers=1,
        max_tasks_per_child=1,
        idle_timeout=None,
    ) as executor:
        assert executor.submit(operator.add, 1, 2).result() == 3
        assert executor.submit(operator.add, 3, 4).result() == 7


def test_max_tasks_per_child_validation():
    with pytest.raises(ValueError):
        InterpreterPoolExecutor(max_tasks_per_child=0)
    with pytest.raises(ValueError):
        InterpreterPoolExecutor(max_tasks_per_child=-1)
    with pytest.raises(TypeError):
        InterpreterPoolExecutor(max_tasks_per_child="1")


@pytest.mark.skipif(sys.version_info < (3, 14), reason="3.14+ only")
def test_interpreter_pool_is_314_plus():
    assert InterpreterPoolExecutor is not None


def test_max_tasks_with_pending_work():
    with InterpreterPoolExecutor(
        max_workers=1,
        max_tasks_per_child=1,
        idle_timeout=None,
    ) as executor:
        futures = [executor.submit(operator.add, i, i) for i in range(10)]
        assert [future.result() for future in futures] == [i * 2 for i in range(10)]
