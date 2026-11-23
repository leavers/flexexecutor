"""
Flexexecutor provides executors that can automatically scale the number of
workers up and down.

Copyright (c) 2020-2026, Leavers.
License: MIT
"""

import asyncio
import atexit
import itertools
import multiprocessing as mp
import multiprocessing.connection
import os
import queue
import sys
import threading
import types
import weakref
from asyncio import AbstractEventLoop
from asyncio import Queue as AsyncQueue
from asyncio.timeouts import timeout
from concurrent.futures import Future, _base
from concurrent.futures.process import BrokenProcessPool
from concurrent.futures.thread import BrokenThreadPool
from functools import partial
from inspect import iscoroutinefunction
from multiprocessing.queues import Queue
from queue import Empty
from threading import Event, Lock, Semaphore, Thread, current_thread
from time import monotonic
from traceback import format_exception
from typing import TYPE_CHECKING
from weakref import WeakKeyDictionary, WeakSet, ref

if TYPE_CHECKING:
    from typing import Callable, ParamSpec, TypeVar

    P = ParamSpec("P")
    T = TypeVar("T")

__all__ = (
    "__version__",
    "AsyncPoolExecutor",
    "AsyncWorkerContext",
    "BrokenProcessPool",
    "BrokenThreadPool",
    "Future",
    "ProcessPoolExecutor",
    "ThreadPoolExecutor",
    "WorkerContext",
)

__version__ = "0.1.0"

# The stdlib only added the BROKEN hook and prepare_context(**ctxkwargs) in 3.14
_PY314_PLUS = sys.version_info >= (3, 14)

if _PY314_PLUS:
    __all__ += ("BrokenInterpreterPool", "InterpreterPoolExecutor")  # noqa: F822

# Type alias for async component (loop, queue) tuple
_AsyncComponent = tuple[AbstractEventLoop, "AsyncQueue[_AsyncWorkItem | None]"]

_threads_queues: WeakKeyDictionary[Thread, queue.SimpleQueue] = WeakKeyDictionary()
_async_executors: WeakSet["AsyncPoolExecutor"] = WeakSet()
_shutdown = False
_global_shutdown_lock = Lock()


def _python_exit():
    global _shutdown
    with _global_shutdown_lock:
        _shutdown = True

    threads_items = list(_threads_queues.items())
    for _, q in threads_items:
        q.put(None)
    for t, _ in threads_items:
        t.join()

    for e in _async_executors:
        e.shutdown()


atexit.register(_python_exit)


class WorkerContext:
    """Worker context for thread pool executor.

    This class provides a way to customize worker thread initialization,
    task execution, and cleanup. It is designed to be compatible with
    Python 3.11+ and follows the design of Python 3.14's ThreadPoolExecutor.

    Example:
        class DatabaseContext(WorkerContext):
            def initialize(self):
                self.db = create_db_connection()

            def finalize(self):
                self.db.close()

            def run(self, task):
                fn, args, kwargs = task
                return fn(self.db, *args, **kwargs)
    """

    @classmethod
    def prepare(
        cls, initializer: "Callable | None" = None, initargs: tuple = ()
    ) -> tuple["Callable[[], WorkerContext]", "Callable[..., tuple]"]:
        """Prepare context factory and task resolver.

        :type initializer: callable, optional
        :param initializer: A callable used to initialize worker threads.

        :type initargs: tuple, optional
        :param initargs: A tuple of arguments to pass to the initializer.

        :return: A tuple of (create_context, resolve_task) functions.
        :rtype: tuple
        """
        if initializer is not None:
            if not callable(initializer):
                raise TypeError("initializer must be a callable")

        def create_context() -> "WorkerContext":
            return cls(initializer, initargs)

        def resolve_task(fn, args, kwargs) -> tuple:
            return (fn, args, kwargs)

        return create_context, resolve_task

    def __init__(self, initializer: "Callable | None" = None, initargs: tuple = ()):
        self.initializer = initializer
        self.initargs = initargs

    def initialize(self) -> None:
        """Initialize the worker context.

        Called once when the worker thread starts.
        """
        if self.initializer is not None:
            self.initializer(*self.initargs)

    def finalize(self) -> None:
        """Finalize the worker context.

        Called once when the worker thread exits.
        """

    def run(self, task: tuple) -> "T":
        """Execute a task.

        :type task: tuple
        :param task: A tuple of (fn, args, kwargs).

        :return: The result of the task execution.
        :rtype: T
        """
        fn, args, kwargs = task
        return fn(*args, **kwargs)


class _WorkItem:
    """A work item for the thread pool executor."""

    def __init__(self, future: Future, task: tuple):
        self.future = future
        self.task = task

    def run(self, ctx: WorkerContext) -> None:
        if not self.future.set_running_or_notify_cancel():
            return

        try:
            result = ctx.run(self.task)
        except BaseException as exc:
            self.future.set_exception(exc)
            # Break a reference cycle with the exception 'exc'
            del self
        else:
            self.future.set_result(result)

    __class_getitem__ = classmethod(types.GenericAlias)


def _worker(
    executor_ref,
    work_queue: queue.SimpleQueue,
    create_context: "Callable[[], WorkerContext]",
    idle_timeout: float,
    max_tasks: int | None = None,
):
    """Worker function for the thread pool executor with idle timeout support."""
    ctx = create_context()
    try:
        ctx.initialize()
    except BaseException:
        _base.LOGGER.critical("Exception in initializer:", exc_info=True)
        executor = executor_ref()
        if executor is not None:  # pragma: no cover
            executor._initializer_failed()
        return

    num_tasks = 0
    idle_tick = monotonic()
    try:
        while True:
            try:
                work_item = work_queue.get(block=True, timeout=0.1)
            except Empty:
                # Check idle timeout
                if idle_timeout >= 0 and monotonic() - idle_tick > idle_timeout:
                    break
                continue

            if work_item is None:
                break

            work_item.run(ctx)
            del work_item

            executor = executor_ref()
            if executor is not None:
                executor._idle_semaphore.release()
            del executor
            idle_tick = monotonic()

            if max_tasks is not None:
                num_tasks += 1
                if num_tasks >= max_tasks:
                    break
    finally:
        ctx.finalize()
        executor = executor_ref()
        if executor is None:
            work_queue.put(None)
        else:
            executor._idle_semaphore.acquire(timeout=0)
            with executor._threads_lock:
                executor._threads.discard(current_thread())
            if not work_queue.empty():
                executor._adjust_thread_count(force=True)
            elif _shutdown or executor._shutdown:
                executor._shutdown = True
                work_queue.put(None)
        del executor


class ThreadPoolExecutor(_base.Executor):
    """A thread pool executor with automatic worker scaling and idle timeout.

    This implementation is compatible with Python 3.11-3.15 and does not depend
    on internal CPython APIs.
    """

    if _PY314_PLUS:
        BROKEN = BrokenThreadPool

    _counter = itertools.count().__next__

    @classmethod
    def prepare_context(
        cls, initializer: "Callable | None" = None, initargs: tuple = ()
    ) -> tuple["Callable[[], WorkerContext]", "Callable[..., tuple]"]:
        """Prepare context factory and task resolver.

        This class method can be overridden to provide a custom WorkerContext.

        :type initializer: callable, optional
        :param initializer: A callable used to initialize worker threads.

        :type initargs: tuple, optional
        :param initargs: A tuple of arguments to pass to the initializer.

        :return: A tuple of (create_context, resolve_task) functions.
        :rtype: tuple
        """
        return WorkerContext.prepare(initializer, initargs)

    def __init__(
        self,
        max_workers: int | None = 1024,
        thread_name_prefix: str = "",
        initializer=None,
        initargs=(),
        idle_timeout: float | None = 60.0,
        **ctxkwargs,
    ):
        """Initializes a new ThreadPoolExecutor instance.

        :type max_workers: int, optional
        :param max_workers: The maximum number of workers to create. Defaults to 1024.

        :type thread_name_prefix: str, optional
        :param thread_name_prefix: An optional name prefix to give our threads.

        :type initializer: callable, optional
        :param initializer: A callable used to initialize worker threads.

        :type initargs: tuple, optional
        :param initargs: A tuple of arguments to pass to the initializer.

        :type idle_timeout: float, optional
        :param idle_timeout: The maximum amount of time (in seconds) that a worker
            thread can remain idle before it is terminated. If set to None or negative
            value, workers will never be terminated. Defaults to 60 seconds.

        :param ctxkwargs: Additional keyword arguments passed to
            ``cls.prepare_context()``. Only supported on Python 3.14+.
        """
        if ctxkwargs and not _PY314_PLUS:
            raise TypeError("ctxkwargs is only supported on Python 3.14+")

        if max_workers is None:
            max_workers = 1024
        if max_workers <= 0:
            raise ValueError("max_workers must be greater than 0")

        self._max_workers = max_workers
        self._work_queue: queue.SimpleQueue[_WorkItem | None] = queue.SimpleQueue()
        self._idle_semaphore = Semaphore(0)
        self._threads: set[Thread] = set()
        self._threads_lock = Lock()
        self._broken = False
        self._shutdown = False
        self._shutdown_lock = Lock()
        self._thread_name_prefix = (
            thread_name_prefix or f"ThreadPoolExecutor-{self._counter()}"
        )

        # Prepare context factory and task resolver
        (
            self._create_worker_context,
            self._resolve_work_item_task,
        ) = type(self).prepare_context(initializer, initargs, **ctxkwargs)

        if idle_timeout is None or idle_timeout < 0:
            self._idle_timeout = -1.0
        else:
            self._idle_timeout = max(0.1, idle_timeout)
        self._max_tasks_per_child = None

    def submit(
        self,
        fn: "Callable[P, T]",
        /,
        *args: "P.args",
        **kwargs: "P.kwargs",
    ) -> "Future[T]":
        if iscoroutinefunction(fn):
            raise TypeError("fn must not be a coroutine function")

        with self._shutdown_lock, _global_shutdown_lock:
            if self._broken:
                # BROKEN only exists on 3.14+, matching the stdlib API surface
                raise getattr(self, "BROKEN", BrokenThreadPool)(self._broken)

            if self._shutdown:
                raise RuntimeError("cannot schedule new futures after shutdown")
            if _shutdown:
                # coverage didn't realize that _shutdown is set, add no cover here
                raise RuntimeError(  # pragma: no cover
                    "cannot schedule new futures after interpreter shutdown"
                )

            f: Future = Future()
            task = self._resolve_work_item_task(fn, args, kwargs)
            w = _WorkItem(f, task)

            self._work_queue.put(w)
            self._adjust_thread_count()
            return f

    submit.__doc__ = _base.Executor.submit.__doc__

    def _adjust_thread_count(self, force: bool = False):
        # If idle threads are available, don't spin new threads
        if not force and self._idle_semaphore.acquire(timeout=0):
            return

        # Remove dead threads
        with self._threads_lock:
            dead_threads = [t for t in self._threads if not t.is_alive()]
            for t in dead_threads:
                self._threads.discard(t)

            # When the executor gets lost, the weakref callback will wake up
            # the worker threads.
            def weakref_cb(_, q=self._work_queue):
                q.put(None)

            num_threads = len(self._threads)
            if num_threads < self._max_workers:
                t = Thread(
                    name=f"{self._thread_name_prefix}_{num_threads}",
                    target=_worker,
                    args=(
                        ref(self, weakref_cb),
                        self._work_queue,
                        self._create_worker_context,
                        self._idle_timeout,
                        getattr(self, "_max_tasks_per_child", None),
                    ),
                )
                t.start()
                self._threads.add(t)
                _threads_queues[t] = self._work_queue

    def _initializer_failed(self):
        with self._shutdown_lock:
            self._broken = (
                "A thread initializer failed, the thread pool is not usable anymore"
            )
            # Drain work queue and mark pending futures failed
            while True:
                try:
                    work_item = self._work_queue.get_nowait()
                except Empty:
                    break
                if work_item is not None:
                    work_item.future.set_exception(
                        getattr(self, "BROKEN", BrokenThreadPool)(self._broken)
                    )

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False):
        with self._shutdown_lock:
            self._shutdown = True
            if cancel_futures:
                # Drain all work items from the queue, and then cancel their
                # associated futures.
                while True:
                    try:
                        work_item = self._work_queue.get_nowait()
                    except Empty:
                        break
                    if work_item is not None:
                        work_item.future.cancel()

            # Send a wake-up to prevent threads calling
            # _work_queue.get(block=True) from permanently blocking.
            self._work_queue.put(None)

        if wait:
            # Workers may start a replacement while draining the shutdown
            # sentinel, so keep joining until no worker remains.
            while True:
                with self._threads_lock:
                    threads = list(self._threads)
                if not threads:
                    break
                for t in threads:
                    t.join()

    shutdown.__doc__ = _base.Executor.shutdown.__doc__


# ProcessPoolExecutor implementation derived from CPython 3.15.
# Copyright 2009 Brian Quinlan. All Rights Reserved.
# Licensed to PSF under a Contributor Agreement.

_process_threads_wakeups = weakref.WeakKeyDictionary()
_process_global_shutdown = False


class _ProcessThreadWakeup:
    def __init__(self):
        self._closed = False
        self._lock = threading.Lock()
        self._reader, self._writer = mp.Pipe(duplex=False)

    def close(self):
        # Please note that we do not take the self._lock when
        # calling clear() (to avoid deadlocking) so this method can
        # only be called safely from the same thread as all calls to
        # clear() even if you hold the lock. Otherwise we
        # might try to read from the closed pipe.
        with self._lock:
            if not self._closed:
                self._closed = True
                self._writer.close()
                self._reader.close()

    def wakeup(self):
        with self._lock:
            if not self._closed:
                self._writer.send_bytes(b"")

    def clear(self):
        if self._closed:
            raise RuntimeError("operation on closed _ProcessThreadWakeup")
        while self._reader.poll():
            self._reader.recv_bytes()


def _process_python_exit():
    global _process_global_shutdown
    _process_global_shutdown = True
    items = list(_process_threads_wakeups.items())
    for _, thread_wakeup in items:
        # call not protected by ProcessPoolExecutor._shutdown_lock
        thread_wakeup.wakeup()
    for t, _ in items:
        t.join()


# Register for `_process_python_exit()` to be called just before joining all
# non-daemon threads. This is used instead of `atexit.register()` for
# compatibility with subinterpreters, which no longer support daemon threads.
# See bpo-39812 for context.
_register_atexit = getattr(threading, "_register_atexit", None)
if _register_atexit is not None:
    _register_atexit(_process_python_exit)  # type: ignore[attr-defined]

# Controls how many more calls than processes will be queued in the call queue.
# A smaller number will mean that processes spend more time idle waiting for
# work while a larger number will make Future.cancel() succeed less frequently
# (Futures in the call queue cannot be cancelled).
_PROCESS_EXTRA_QUEUED_CALLS = 1


# On Windows, WaitForMultipleObjects is used to wait for processes to finish.
# It can wait on, at most, 64 objects. There is an overhead of three objects:
# - the result queue reader
# - the thread wakeup reader
# - the SIGINT handler
_PROCESS_MAX_WINDOWS_WORKERS = 64 - 3

# Hack to embed stringification of remote traceback in local traceback


class _RemoteTraceback(Exception):
    def __init__(self, tb):
        self.tb = tb

    def __str__(self):
        return self.tb


class _ExceptionWithTraceback:
    def __init__(self, exc, tb):
        tb = "".join(format_exception(type(exc), exc, tb))
        self.exc = exc
        # Traceback object needs to be garbage-collected as its frames
        # contain references to all the objects in the exception scope
        self.exc.__traceback__ = None
        self.tb = '\n"""\n%s"""' % tb

    def __reduce__(self):
        return _rebuild_exc, (self.exc, self.tb)


def _rebuild_exc(exc, tb):
    exc.__cause__ = _RemoteTraceback(tb)
    return exc


class _ProcessWorkItem(object):
    def __init__(self, future, fn, args, kwargs):
        self.future = future
        self.fn = fn
        self.args = args
        self.kwargs = kwargs


class _ProcessResultItem(object):
    def __init__(
        self,
        work_id,
        exception=None,
        result=None,
        exit_pid=None,
        retire_reason=None,
        had_task=False,
    ):
        self.work_id = work_id
        self.exception = exception
        self.result = result
        self.exit_pid = exit_pid
        self.retire_reason = retire_reason
        self.had_task = had_task


class _ProcessCallItem(object):
    def __init__(self, work_id, fn, args, kwargs):
        self.work_id = work_id
        self.fn = fn
        self.args = args
        self.kwargs = kwargs


class _ProcessSafeQueue(Queue):
    """Safe Queue set exception to the future object linked to a job"""

    def __init__(self, max_size=0, *, ctx, pending_work_items, thread_wakeup):
        self.pending_work_items = pending_work_items
        self.thread_wakeup = thread_wakeup
        super().__init__(max_size, ctx=ctx)

    def _on_queue_feeder_error(self, e, obj):
        if isinstance(obj, _ProcessCallItem):
            tb = format_exception(type(e), e, e.__traceback__)
            e.__cause__ = _RemoteTraceback('\n"""\n{}"""'.format("".join(tb)))
            work_item = self.pending_work_items.pop(obj.work_id, None)
            self.thread_wakeup.wakeup()
            # work_item can be None if another process terminated. In this
            # case, the executor_manager_thread fails all work_items
            # with BrokenProcessPool
            if work_item is not None:
                work_item.future.set_exception(e)
        else:
            getattr(super(), "_on_queue_feeder_error")(e, obj)  # type: ignore[attr-defined]


def _process_chunk(fn, chunk):
    """Processes a chunk of an iterable passed to map.

    Runs the function passed to map() on a chunk of the
    iterable passed to map.

    This function is run in a separate process.

    """
    return [fn(*args) for args in chunk]


def _process_batched(iterable, n):
    """itertools.batched with a Python 3.11 fallback."""
    if hasattr(itertools, "batched"):
        yield from itertools.batched(iterable, n)
        return
    iterator = iter(iterable)
    while batch := tuple(itertools.islice(iterator, n)):
        yield batch


def _sendback_result(
    result_queue,
    work_id,
    result=None,
    exception=None,
    exit_pid=None,
    retire_reason=None,
    had_task=False,
):
    """Safely send back the given result or exception"""
    item = _ProcessResultItem(
        work_id,
        result=result,
        exception=exception,
        exit_pid=exit_pid,
        retire_reason=retire_reason,
        had_task=had_task,
    )
    try:
        result_queue.put(item)
    except BaseException as e:
        exc = _ExceptionWithTraceback(e, e.__traceback__)
        result_queue.put(
            _ProcessResultItem(
                work_id,
                exception=exc,
                exit_pid=exit_pid,
                retire_reason=retire_reason,
                had_task=had_task,
            )
        )


def _process_worker(
    call_queue,
    result_queue,
    initializer,
    initargs,
    max_tasks=None,
    idle_timeout=-1.0,
):
    """Evaluates calls from call_queue and places the results in result_queue.

    This worker is run in a separate process.

    :type call_queue: multiprocessing.queues.Queue
    :param call_queue: A ctx.Queue of _ProcessCallItems that will be read and
        evaluated by the worker.

    :type result_queue: multiprocessing.queues.Queue
    :param result_queue: A ctx.Queue of _ProcessResultItems that will be written
        to by the worker.

    :type initializer: callable, optional
    :param initializer: A callable initializer, or None.

    :type initargs: tuple
    :param initargs: A tuple of args for the initializer.

    :type max_tasks: int, optional
    :param max_tasks: Maximum number of tasks before the worker exits; None
        means unlimited.

    :type idle_timeout: float, optional
    :param idle_timeout: Seconds without a task before the worker exits;
        negative disables idle retirement.
    """
    if initializer is not None:
        try:
            initializer(*initargs)
        except BaseException:
            _base.LOGGER.critical("Exception in initializer:", exc_info=True)
            # The parent will notice that the process stopped and
            # mark the pool broken
            return
    num_tasks = 0
    exit_pid = None
    retire_reason = None
    had_task = False
    last_active = monotonic()
    while True:
        try:
            if idle_timeout >= 0:
                call_item = call_queue.get(block=True, timeout=0.1)
            else:
                call_item = call_queue.get(block=True)
        except queue.Empty:
            if idle_timeout >= 0 and monotonic() - last_active > idle_timeout:
                _sendback_result(
                    result_queue,
                    None,
                    exit_pid=os.getpid(),
                    retire_reason="idle",
                    had_task=had_task,
                )
                return
            continue

        if call_item is None:
            # Wake up queue management thread
            result_queue.put(os.getpid())
            return

        had_task = True
        if max_tasks is not None:
            num_tasks += 1
            if num_tasks >= max_tasks:
                exit_pid = os.getpid()
                retire_reason = "max_tasks"

        try:
            r = call_item.fn(*call_item.args, **call_item.kwargs)
        except BaseException as e:
            exc = _ExceptionWithTraceback(e, e.__traceback__)
            _sendback_result(
                result_queue,
                call_item.work_id,
                exception=exc,
                exit_pid=exit_pid,
                retire_reason=retire_reason,
            )
        else:
            _sendback_result(
                result_queue,
                call_item.work_id,
                result=r,
                exit_pid=exit_pid,
                retire_reason=retire_reason,
            )
            del r

        # Liberate the resource as soon as possible, to avoid holding onto
        # open files or shared memory that is not needed anymore
        del call_item
        last_active = monotonic()

        if exit_pid is not None:
            return


def _spawn_process_worker(
    mp_context,
    call_queue,
    result_queue,
    initializer,
    initargs,
    max_tasks_per_child,
    idle_timeout,
    processes,
):
    """Start one worker process and record it in *processes* by pid."""
    p = mp_context.Process(
        target=_process_worker,
        args=(
            call_queue,
            result_queue,
            initializer,
            initargs,
            max_tasks_per_child,
            idle_timeout,
        ),
    )
    p.start()
    processes[p.pid] = p


class _ProcessManagerThread(threading.Thread):
    """Manages the communication between this process and the worker processes.

    The manager is run in a local thread.

    :type executor: ProcessPoolExecutor
    :param executor: A reference to the ProcessPoolExecutor that owns this
        thread. A weakref will be owned by the manager as well as references
        to internal objects used to introspect the state of the executor.
    """

    def __init__(self, executor):
        # Store references to necessary internals of the executor.

        # A _ProcessThreadWakeup to allow waking up the queue_manager_thread from the
        # main Thread and avoid deadlocks caused by permanently locked queues.
        self.thread_wakeup = executor._executor_manager_thread_wakeup
        self.shutdown_lock = executor._shutdown_lock

        # A weakref.ref to the ProcessPoolExecutor that owns this thread. Used
        # to determine if the ProcessPoolExecutor has been garbage collected
        # and that the manager can exit.
        # When the executor gets garbage collected, the weakref callback
        # will wake up the queue management thread so that it can terminate
        # if there is no pending work item.
        def weakref_cb(
            _,
            thread_wakeup=self.thread_wakeup,
            mp_util_debug=getattr(getattr(mp, "util", None), "debug", lambda *_: None),
        ):
            mp_util_debug(
                "Executor collected: triggering callback for QueueManager wakeup"
            )
            thread_wakeup.wakeup()

        self.executor_reference = weakref.ref(executor, weakref_cb)

        # A list of the ctx.Process instances used as workers.
        self.processes = executor._processes

        # A ctx.Queue that will be filled with _ProcessCallItems derived from
        # _ProcessWorkItems for processing by the process workers.
        self.call_queue = executor._call_queue

        # A ctx.SimpleQueue of _ProcessResultItems generated by the process workers.
        self.result_queue = executor._result_queue

        # A queue.Queue of work ids e.g. Queue([5, 6, ...]).
        self.work_ids_queue = executor._work_ids

        # Maximum number of tasks a worker process can execute before
        # exiting safely
        self.max_tasks_per_child = executor._max_tasks_per_child

        # Idle timeout used by worker processes; -1 disables it.
        self.idle_timeout = executor._idle_timeout

        # gh-119592: Needed to size worker replacement, and immutable, so
        # keep a copy rather than reading it back through the executor
        # weakref.  The rest of the spawn configuration is deliberately NOT
        # copied here: holding user-provided objects (initializer,
        # initargs, mp_context) in this always-reachable running thread
        # could keep the executor itself reachable through them, breaking
        # garbage-collection-triggered shutdown.
        self.max_workers = executor._max_workers

        # A dict mapping work ids to _ProcessWorkItems e.g.
        #     {5: <_ProcessWorkItem...>, 6: <_ProcessWorkItem...>, ...}
        self.pending_work_items = executor._pending_work_items

        super().__init__()

    def run(self):
        # Main loop for the executor manager thread.

        while True:
            # gh-109047: During Python finalization, self.call_queue.put()
            # creation of a thread can fail with RuntimeError.
            try:
                self.add_call_item_to_queue()
            except BaseException as exc:
                cause = format_exception(exc)
                self.terminate_broken(cause)
                return

            result_item, is_broken, cause = self.wait_result_broken_or_wakeup()

            if is_broken:
                self.terminate_broken(cause)
                return
            if result_item is not None:
                retire_reason = getattr(result_item, "retire_reason", None)
                had_task = getattr(result_item, "had_task", False)
                self.process_result_item(result_item)

                process_exited = getattr(result_item, "exit_pid", None) is not None
                if process_exited:
                    p = self.processes.pop(result_item.exit_pid)
                    p.join()

                # Delete reference to result_item to avoid keeping references
                # while waiting on new results.
                del result_item

                if process_exited:
                    with self.shutdown_lock:
                        if retire_reason == "idle":
                            broken = self._replace_idle_worker(had_task)
                        else:
                            broken = self._replace_dead_worker()
                    if broken is not None:
                        self.terminate_broken(*broken)
                        return
                elif executor := self.executor_reference():
                    executor._idle_worker_semaphore.release()
                    del executor

            if self.is_shutting_down():
                self.flag_executor_shutting_down()

                # When only canceled futures remain in pending_work_items, our
                # next call to wait_result_broken_or_wakeup would hang forever.
                # This makes sure we have some running futures or none at all.
                self.add_call_item_to_queue()

                # Since no new work items can be added, it is safe to shutdown
                # this thread if there are no pending work items.
                if not self.pending_work_items:
                    self.join_executor_internals()
                    return

    def _replace_dead_worker(self):
        """Spawn a replacement for a worker that exited at its
        max_tasks_per_child limit.  Called under self.shutdown_lock.

        Returns None while the pool can still make progress, otherwise a
        (cause, message) tuple describing why the remaining work items can
        never run, so that run() can fail their futures.
        """
        assert self.shutdown_lock.locked()
        cause = None
        message = None
        executor = self.executor_reference()
        if executor is None:
            # gh-152967: The executor was garbage collected; nothing can
            # spawn a replacement worker for it anymore.
            message = (
                "The ProcessPoolExecutor was garbage collected with "
                "work pending after its last worker process exited "
                "upon reaching max_tasks_per_child; the pending work "
                "can never be run."
            )
        elif executor._force_shutting_down:
            # terminate_workers()/kill_workers() is tearing the pool down;
            # a replacement worker would escape the kill and run work
            # items that were enqueued before it.
            message = (
                "A worker process exited while the pool was being "
                "forcefully shut down; work that was still enqueued "
                "will not be run."
            )
        elif self.pending_work_items or not self.is_shutting_down():
            # gh-115634: Do not consult the executor's
            # _idle_worker_semaphore here: it counts task completions, not
            # idle workers, so it can hold a stale token released by the
            # now-dead worker.  Trusting such a token would leave the pool
            # a worker short, deadlocking once all workers reach their
            # task limit.  Spawning from this (manager) thread is safe
            # despite gh-90622 because max_tasks_per_child is rejected for
            # the "fork" start method.
            if len(self.processes) < self.max_workers:
                # gh-119592: Spawn using state owned by this thread and
                # configuration read through the live weakref (which
                # shutdown() never clears), not the executor state that
                # shutdown(wait=False) clears concurrently.
                try:
                    _spawn_process_worker(
                        executor._mp_context,
                        self.call_queue,
                        self.result_queue,
                        executor._initializer,
                        executor._initargs,
                        self.max_tasks_per_child,
                        self.idle_timeout,
                        self.processes,
                    )
                except Exception as exc:
                    # While other workers remain the pool has merely lost
                    # capacity and they keep draining the queue; with none
                    # left the failure is reported below.
                    cause = format_exception(exc)
                    message = (
                        "A replacement worker process could not be "
                        "started, leaving the pool without workers "
                        "to run the remaining work."
                    )
        del executor

        if not self.processes and (self.pending_work_items or cause is not None):
            # No worker processes remain and no replacement can be
            # spawned: any remaining work items can never run.  A spawn
            # failure breaks the pool even with nothing pending; leaving
            # a zero-worker pool alive would hang a later submit() on a
            # stale _idle_worker_semaphore token instead of raising.
            return (cause, message)
        return None

    def _replace_idle_worker(self, had_task):
        """Handle a worker that exited because it was idle.

        Called under self.shutdown_lock.  Unlike max_tasks_per_child
        retirement, an idle worker is only replaced while work is pending.
        """
        assert self.shutdown_lock.locked()
        cause = None
        message = None
        executor = self.executor_reference()
        if had_task and executor is not None:
            # The worker released one token after its last completed task.
            # Drop it so a later submit() does not mistake this exited
            # worker for an idle one.  A worker that never ran a task left
            # no token of its own, so do not consume another worker's.
            executor._idle_worker_semaphore.acquire(blocking=False)

        if executor is None:
            message = (
                "The ProcessPoolExecutor was garbage collected with "
                "work pending after its last idle worker process exited; "
                "the pending work can never be run."
            )
        elif executor._force_shutting_down:
            message = (
                "A worker process exited while the pool was being "
                "forcefully shut down; work that was still enqueued "
                "will not be run."
            )
        elif self.pending_work_items and len(self.processes) < self.max_workers:
            try:
                _spawn_process_worker(
                    executor._mp_context,
                    self.call_queue,
                    self.result_queue,
                    executor._initializer,
                    executor._initargs,
                    self.max_tasks_per_child,
                    self.idle_timeout,
                    self.processes,
                )
            except Exception as exc:
                cause = format_exception(exc)
                message = (
                    "A replacement worker process could not be started, "
                    "leaving the pool without workers to run the remaining "
                    "work."
                )
        del executor

        if not self.processes and (self.pending_work_items or cause is not None):
            return (cause, message)
        return None

    def add_call_item_to_queue(self):
        # Fills call_queue with _ProcessWorkItems from pending_work_items.
        # This function never blocks.
        while True:
            if self.call_queue.full():
                return
            try:
                work_id = self.work_ids_queue.get(block=False)
            except queue.Empty:
                return
            else:
                work_item = self.pending_work_items[work_id]

                if work_item.future.set_running_or_notify_cancel():
                    self.call_queue.put(
                        _ProcessCallItem(
                            work_id, work_item.fn, work_item.args, work_item.kwargs
                        ),
                        block=True,
                    )
                else:
                    del self.pending_work_items[work_id]
                    continue

    def wait_result_broken_or_wakeup(self):
        # Wait for a result to be ready in the result_queue while checking
        # that all worker processes are still running, or for a wake up
        # signal send. The wake up signals come either from new tasks being
        # submitted, from the executor being shutdown/gc-ed, or from the
        # shutdown of the python interpreter.
        result_reader = self.result_queue._reader
        assert not self.thread_wakeup._closed
        wakeup_reader = self.thread_wakeup._reader
        readers = [result_reader, wakeup_reader]
        worker_sentinels = [p.sentinel for p in list(self.processes.values())]
        ready = getattr(mp, "connection").wait(readers + worker_sentinels)

        cause = None
        is_broken = True
        result_item = None
        if result_reader in ready:
            try:
                result_item = result_reader.recv()
                is_broken = False
            except BaseException as exc:
                cause = format_exception(exc)

        elif wakeup_reader in ready:
            is_broken = False

        self.thread_wakeup.clear()

        return result_item, is_broken, cause

    def process_result_item(self, result_item):
        # Process the received a result_item. This can be either the PID of a
        # worker that exited gracefully or a _ProcessResultItem

        if isinstance(result_item, int):
            assert self.is_shutting_down()
            p = self.processes.pop(result_item)
            p.join()
            return

        # Received a _ProcessResultItem so mark the future as completed.
        if result_item.work_id is not None:
            work_item = self.pending_work_items.pop(result_item.work_id, None)
            # work_item can be None if another process terminated (see above)
            if work_item is not None:
                if result_item.exception is not None:
                    work_item.future.set_exception(result_item.exception)
                else:
                    work_item.future.set_result(result_item.result)

    def is_shutting_down(self):
        # Check whether we should start shutting down the executor.
        executor = self.executor_reference()
        # No more work items can be added if:
        #   - The interpreter is shutting down OR
        #   - The executor that owns this worker has been collected OR
        #   - The executor that owns this worker has been shutdown.
        return _process_global_shutdown or executor is None or executor._shutdown_thread

    def _terminate_broken(self, cause, bpe_message=None):
        # Terminate the executor because it is in a broken state. The cause
        # argument can be used to display more information on the error that
        # lead the executor into becoming broken.  bpe_message overrides the
        # default message on the BrokenProcessPool set on pending futures.

        # Mark the process pool broken so that submits fail right now.
        executor = self.executor_reference()
        if executor is not None:
            executor._broken = (
                "A child process terminated "
                "abruptly, the process pool is not "
                "usable anymore"
            )
            executor._shutdown_thread = True
            del executor

        # All pending tasks are to be marked failed with a
        # BrokenProcessPool error, as separate instances to avoid sharing
        # a traceback (gh-101267).
        cause_str = None
        if cause is not None:
            cause_str = "".join(cause)
        else:
            # No cause known, so report any processes that have
            # terminated with nonzero exit codes, e.g. from a
            # segfault. Multiple may terminate simultaneously,
            # so include all of them in the traceback.
            errors = []
            for p in self.processes.values():
                if p.exitcode is not None and p.exitcode != 0:
                    errors.append(
                        f"Process {p.pid} terminated abruptly "
                        f"with exit code {p.exitcode}"
                    )
            if errors:
                cause_str = "\n".join(errors)
        cause_tb = f"\n'''\n{cause_str}'''" if cause_str else None

        if bpe_message is None:
            bpe_message = (
                "A process in the process pool was terminated "
                "abruptly while the future was running or pending."
            )
        # Mark pending tasks as failed.
        for _work_id, work_item in self.pending_work_items.items():
            bpe = BrokenProcessPool(bpe_message)
            if cause_tb is not None:
                bpe.__cause__ = _RemoteTraceback(cause_tb)
            try:
                work_item.future.set_exception(bpe)
            except _base.InvalidStateError:
                # set_exception() fails if the future is cancelled: ignore it.
                # Trying to check if the future is cancelled before calling
                # set_exception() would leave a race condition if the future is
                # cancelled between the check and set_exception().
                pass
            # Delete references to object. See issue16284
            del work_item
        self.pending_work_items.clear()

        # Terminate remaining workers forcibly: the queues or their
        # locks may be in a dirty state and block forever.
        for p in self.processes.values():
            p.terminate()

        if hasattr(self.call_queue, "_terminate_broken"):
            self.call_queue._terminate_broken()
        else:  # Python 3.11
            try:
                self.call_queue.close()
            finally:
                self.call_queue.join_thread()

        # clean up resources
        self._join_executor_internals(broken=True)

    def terminate_broken(self, cause, bpe_message=None):
        with self.shutdown_lock:
            self._terminate_broken(cause, bpe_message)

    def flag_executor_shutting_down(self):
        # Flag the executor as shutting down and cancel remaining tasks if
        # requested as early as possible if it is not gc-ed yet.
        executor = self.executor_reference()
        if executor is not None:
            executor._shutdown_thread = True
            # Cancel pending work items if requested.
            if executor._cancel_pending_futures:
                # Cancel all pending futures and update pending_work_items
                # to only have futures that are currently running.
                new_pending_work_items = {}
                for work_id, work_item in self.pending_work_items.items():
                    if not work_item.future.cancel():
                        new_pending_work_items[work_id] = work_item
                self.pending_work_items = new_pending_work_items
                # Drain work_ids_queue since we no longer need to
                # add items to the call queue.
                while True:
                    try:
                        self.work_ids_queue.get_nowait()
                    except queue.Empty:
                        break
                # Make sure we do this only once to not waste time looping
                # on running processes over and over.
                executor._cancel_pending_futures = False

    def shutdown_workers(self):
        n_children_to_stop = self.get_n_children_alive()
        n_sentinels_sent = 0
        # Send the right number of sentinels, to make sure all children are
        # properly terminated.
        while n_sentinels_sent < n_children_to_stop and self.get_n_children_alive() > 0:
            for _ in range(n_children_to_stop - n_sentinels_sent):
                try:
                    self.call_queue.put_nowait(None)
                    n_sentinels_sent += 1
                except queue.Full:
                    break

    def join_executor_internals(self):
        with self.shutdown_lock:
            self._join_executor_internals()

    def _join_executor_internals(self, broken=False):
        # If broken, call_queue was closed and so can no longer be used.
        if not broken:
            self.shutdown_workers()

        # Release the queue's resources as soon as possible.
        self.call_queue.close()
        self.call_queue.join_thread()
        self.thread_wakeup.close()

        # If .join() is not called on the created processes then
        # some ctx.Queue methods may deadlock on Mac OS X.
        for p in self.processes.values():
            if broken:
                p.terminate()
            p.join()

    def get_n_children_alive(self):
        # This is an upper bound on the number of children alive.
        return sum(p.is_alive() for p in self.processes.values())


_process_system_limits_checked = False
_process_system_limited = None


def _check_process_system_limits():
    global _process_system_limits_checked, _process_system_limited
    if _process_system_limits_checked:
        if _process_system_limited:
            raise NotImplementedError(_process_system_limited)
    _process_system_limits_checked = True
    try:
        import multiprocessing.synchronize  # noqa: F401
    except ImportError:
        _process_system_limited = (
            "This Python build lacks multiprocessing.synchronize, usually due "
            "to named semaphores being unavailable on this platform."
        )
        raise NotImplementedError(_process_system_limited)
    try:
        nsems_max = os.sysconf("SC_SEM_NSEMS_MAX")
    except (AttributeError, ValueError):
        # sysconf not available or setting not available
        return
    if nsems_max == -1:
        # indetermined limit, assume that limit is determined
        # by available memory only
        return
    if nsems_max >= 256:
        # minimum number of semaphores available
        # according to POSIX
        return
    _process_system_limited = (
        "system provides too few semaphores (%d available, 256 necessary)" % nsems_max
    )
    raise NotImplementedError(_process_system_limited)


def _chain_from_iterable_of_lists(iterable):
    """
    Specialized implementation of itertools.chain.from_iterable.
    Each item in *iterable* should be a list.  This function is
    careful not to keep references to yielded objects.
    """
    for element in iterable:
        element.reverse()
        while element:
            yield element.pop()


_PROCESS_TERMINATE = "terminate"
_PROCESS_KILL = "kill"

_PROCESS_SHUTDOWN_CALLBACK_OPERATION = {_PROCESS_TERMINATE, _PROCESS_KILL}


class ProcessPoolExecutor(_base.Executor):
    def __init__(
        self,
        max_workers=None,
        mp_context=None,
        initializer=None,
        initargs=(),
        *,
        max_tasks_per_child=None,
        idle_timeout: float | None = 60.0,
    ):
        """Initializes a new ProcessPoolExecutor instance.

        :type max_workers: int, optional
        :param max_workers: The maximum number of processes that can be used to
            execute the given calls. If None or not given then as many worker
            processes will be created as the machine has processors.

        :type mp_context: multiprocessing.context.BaseContext, optional
        :param mp_context: A multiprocessing context to launch the workers
            created using the multiprocessing.get_context('start method')
            API. This object should provide SimpleQueue, Queue and Process.

        :type initializer: callable, optional
        :param initializer: A callable used to initialize worker processes.

        :type initargs: tuple, optional
        :param initargs: A tuple of arguments to pass to the initializer.

        :type max_tasks_per_child: int, optional
        :param max_tasks_per_child: The maximum number of tasks a worker
            process can complete before it will exit and be replaced with a
            fresh worker process. The default of None means worker process
            will live as long as the executor. Requires a non-'fork'
            mp_context start method. When given, we default to using 'spawn'
            if no mp_context is supplied.

        :type idle_timeout: float, optional
        :param idle_timeout: The maximum number of seconds a worker process may
            remain idle before it exits. If None or negative, workers are never
            terminated for being idle. Defaults to 60 seconds. Requires a
            non-'fork' start method.
        """
        _check_process_system_limits()

        if idle_timeout is None or idle_timeout < 0:
            self._idle_timeout = -1.0
        else:
            self._idle_timeout = max(0.1, idle_timeout)

        if max_workers is None:
            cpu_count = getattr(os, "process_cpu_count", None) or os.cpu_count
            self._max_workers = cpu_count() or 1
            if sys.platform == "win32":
                self._max_workers = min(_PROCESS_MAX_WINDOWS_WORKERS, self._max_workers)
        else:
            if max_workers <= 0:
                raise ValueError("max_workers must be greater than 0")
            elif sys.platform == "win32" and max_workers > _PROCESS_MAX_WINDOWS_WORKERS:
                raise ValueError(
                    f"max_workers must be <= {_PROCESS_MAX_WINDOWS_WORKERS}"
                )

            self._max_workers = max_workers

        if mp_context is None:
            if max_tasks_per_child is not None:
                mp_context = mp.get_context("spawn")
            elif self._idle_timeout >= 0:
                if sys.platform in ("win32", "darwin"):
                    mp_context = mp.get_context("spawn")
                else:
                    try:
                        mp_context = mp.get_context("forkserver")
                    except ValueError:
                        mp_context = mp.get_context("spawn")
            else:
                mp_context = mp.get_context()
        self._mp_context = mp_context
        start_method = self._mp_context.get_start_method(allow_none=False)

        # https://github.com/python/cpython/issues/90622
        self._safe_to_dynamically_spawn_children = start_method != "fork"

        if initializer is not None and not callable(initializer):
            raise TypeError("initializer must be a callable")
        self._initializer = initializer
        self._initargs = initargs

        if max_tasks_per_child is not None:
            if not isinstance(max_tasks_per_child, int):
                raise TypeError("max_tasks_per_child must be an integer")
            elif max_tasks_per_child <= 0:
                raise ValueError("max_tasks_per_child must be >= 1")
            if start_method == "fork":
                # https://github.com/python/cpython/issues/90622
                raise ValueError(
                    "max_tasks_per_child is incompatible with"
                    " the 'fork' multiprocessing start method;"
                    " supply a different mp_context."
                )
        self._max_tasks_per_child = max_tasks_per_child

        if self._idle_timeout >= 0 and start_method == "fork":
            raise ValueError(
                "idle_timeout is incompatible with the 'fork' multiprocessing "
                "start method; supply a different mp_context or set "
                "idle_timeout=None."
            )

        # Management thread
        self._executor_manager_thread = None

        # Map of pids to processes
        self._processes = {}

        # Shutdown is a two-step process.
        self._shutdown_thread = False
        self._shutdown_lock = threading.Lock()
        self._idle_worker_semaphore = threading.Semaphore(0)
        self._broken = False
        self._queue_count = 0
        self._pending_work_items = {}
        self._cancel_pending_futures = False
        self._force_shutting_down = False

        # _ProcessThreadWakeup is a communication channel used to interrupt the wait
        # of the main loop of executor_manager_thread from another thread (e.g.
        # when calling executor.submit or executor.shutdown). We do not use the
        # _result_queue to send wakeup signals to the executor_manager_thread
        # as it could result in a deadlock if a worker process dies with the
        # _result_queue write lock still acquired.
        #
        # Care must be taken to only call clear and close from the
        # executor_manager_thread, since _ProcessThreadWakeup.clear() is not protected
        # by a lock.
        self._executor_manager_thread_wakeup = _ProcessThreadWakeup()

        # Create communication channels for the executor
        # Make the call queue slightly larger than the number of processes to
        # prevent the worker processes from idling. But don't make it too big
        # because futures in the call queue cannot be cancelled.
        queue_size = self._max_workers + _PROCESS_EXTRA_QUEUED_CALLS
        self._call_queue = _ProcessSafeQueue(
            max_size=queue_size,
            ctx=self._mp_context,
            pending_work_items=self._pending_work_items,
            thread_wakeup=self._executor_manager_thread_wakeup,
        )
        # Killed worker processes can produce spurious "broken pipe"
        # tracebacks in the queue's own worker thread. But we detect killed
        # processes anyway, so silence the tracebacks.
        setattr(self._call_queue, "_ignore_epipe", True)  # type: ignore[attr-defined]
        self._result_queue = self._mp_context.SimpleQueue()
        self._work_ids = queue.Queue()

    def _start_executor_manager_thread(self):
        if self._executor_manager_thread is None:
            # Start the processes so that their sentinels are known.
            if not self._safe_to_dynamically_spawn_children:  # ie, using fork.
                self._launch_processes()
            self._executor_manager_thread = _ProcessManagerThread(self)
            self._executor_manager_thread.start()
            _process_threads_wakeups[self._executor_manager_thread] = (
                self._executor_manager_thread_wakeup
            )

    def _adjust_process_count(self):
        # gh-132969: avoid error when state is reset and executor is still running,
        # which will happen when shutdown(wait=False) is called.
        if self._processes is None:
            return

        # gh-152967: A forceful shutdown is in progress; a worker spawned
        # here could escape its process snapshot and keep running work.
        if self._force_shutting_down:
            return

        # if there's an idle process, we don't need to spawn a new one.
        if self._idle_worker_semaphore.acquire(blocking=False):
            return

        process_count = len(self._processes)
        if process_count < self._max_workers:
            # gh-90622: spawning a child via fork while another thread is
            # running can deadlock in the child.  submit() only calls this
            # method when using a non-fork start method.
            assert (
                self._safe_to_dynamically_spawn_children
                or not self._executor_manager_thread
            ), "https://github.com/python/cpython/issues/90622"
            self._spawn_process()

    def _launch_processes(self):
        # https://github.com/python/cpython/issues/90622
        assert not self._executor_manager_thread, (
            "Processes cannot be fork()ed after the thread has started, "
            "deadlock in the child processes could result."
        )
        processes = self._processes
        assert processes is not None
        for _ in range(len(processes), self._max_workers):
            self._spawn_process()

    def _spawn_process(self):
        _spawn_process_worker(
            self._mp_context,
            self._call_queue,
            self._result_queue,
            self._initializer,
            self._initargs,
            self._max_tasks_per_child,
            self._idle_timeout,
            self._processes,
        )

    def submit(self, fn, /, *args, **kwargs):
        with self._shutdown_lock:
            if self._broken:
                raise BrokenProcessPool(self._broken)
            if self._shutdown_thread:
                raise RuntimeError("cannot schedule new futures after shutdown")
            if _process_global_shutdown:
                raise RuntimeError(
                    "cannot schedule new futures after interpreter shutdown"
                )

            f = _base.Future()
            w = _ProcessWorkItem(f, fn, args, kwargs)

            self._pending_work_items[self._queue_count] = w
            self._work_ids.put(self._queue_count)
            self._queue_count += 1
            # Wake up queue management thread
            wakeup = self._executor_manager_thread_wakeup
            if wakeup is not None:
                wakeup.wakeup()

            if self._safe_to_dynamically_spawn_children:
                self._adjust_process_count()
            self._start_executor_manager_thread()
            return f

    submit.__doc__ = _base.Executor.submit.__doc__

    def _map(self, fn, iterables, timeout, chunksize, buffersize):
        """Returns an iterator equivalent to map(fn, iter).

        :type fn: callable
        :param fn: A callable that will take as many arguments as there are
            passed iterables.

        :type timeout: float, optional
        :param timeout: The maximum number of seconds to wait. If None, then
            there is no limit on the wait time.

        :type chunksize: int
        :param chunksize: If greater than one, the iterables will be chopped
            into chunks of size chunksize and submitted to the process pool.
            If set to one, the items in the list will be sent one at a time.

        :type buffersize: int, optional
        :param buffersize: The number of submitted tasks whose results have not
            yet been yielded. If the buffer is full, iteration over the
            iterables pauses until a result is yielded from the buffer. If
            None, all input elements are eagerly collected, and a task is
            submitted for each.

        :return: An iterator equivalent to map(func, *iterables) but the calls
            may be evaluated out-of-order.
        :rtype: Iterator

        :raises TimeoutError: If the entire result iterator could not be
            generated before the given timeout.
        :raises Exception: If fn(*args) raises for any values.
        """
        if chunksize < 1:
            raise ValueError("chunksize must be >= 1.")

        chunks = _process_batched(zip(*iterables), chunksize)
        if _PY314_PLUS:
            results = super().map(
                partial(_process_chunk, fn),
                chunks,
                timeout=timeout,
                buffersize=buffersize,
            )
        else:
            results = super().map(
                partial(_process_chunk, fn),
                chunks,
                timeout=timeout,
            )
        return _chain_from_iterable_of_lists(results)

    def map(self, fn, *iterables, timeout=None, chunksize=1, buffersize=None):
        if not _PY314_PLUS and buffersize is not None:
            raise TypeError("map() got an unexpected keyword argument 'buffersize'")
        return self._map(fn, iterables, timeout, chunksize, buffersize)

    def shutdown(self, wait=True, *, cancel_futures=False):
        with self._shutdown_lock:
            self._cancel_pending_futures = cancel_futures
            self._shutdown_thread = True
            if self._executor_manager_thread_wakeup is not None:
                # Wake up queue management thread
                self._executor_manager_thread_wakeup.wakeup()

        if self._executor_manager_thread is not None and wait:
            self._executor_manager_thread.join()
        # To reduce the risk of opening too many files, remove references to
        # objects that use file descriptors.
        self._executor_manager_thread = None
        self._call_queue = None
        if self._result_queue is not None and wait:
            self._result_queue.close()
        self._result_queue = None
        self._processes = None
        self._executor_manager_thread_wakeup = None

    shutdown.__doc__ = _base.Executor.shutdown.__doc__

    def _force_shutdown(self, operation):
        """Attempts to terminate or kill the executor's workers based off the
        given operation. Iterates through all of the current processes and
        performs the relevant task if the process is still alive.

        After terminating workers, the pool will be in a broken state
        and no longer usable (for instance, new tasks should not be
        submitted).
        """
        if operation not in _PROCESS_SHUTDOWN_CALLBACK_OPERATION:
            raise ValueError(f"Unsupported operation: {operation!r}")

        # gh-152967: Stop the manager thread from spawning replacement
        # workers before we copy the processes to signal: a worker spawned
        # after the copy would survive the loop below and run enqueued
        # work items.  Taking the lock orders this against the manager's
        # worker replacement, which runs under the same lock.
        with self._shutdown_lock:
            self._force_shutting_down = True

        processes = {}
        if self._processes:
            processes = self._processes.copy()

        # shutdown will invalidate ._processes, so we copy it right before
        # calling. If we waited here, we would deadlock if a process decides not
        # to exit.
        self.shutdown(wait=False, cancel_futures=True)

        if not processes:
            return

        for proc in processes.values():
            try:
                if not proc.is_alive():
                    continue
            except ValueError:
                # The process is already exited/closed out.
                continue

            try:
                if operation == _PROCESS_TERMINATE:
                    proc.terminate()
                elif operation == _PROCESS_KILL:
                    proc.kill()
            except ProcessLookupError:
                # The process just ended before our signal
                continue

    if _PY314_PLUS:

        def terminate_workers(self):
            """Attempts to terminate the executor's workers.
            Iterates through all of the current worker processes and terminates
            each one that is still alive.

            After terminating workers, the pool will be in a broken state
            and no longer usable (for instance, new tasks should not be
            submitted).
            """
            return self._force_shutdown(operation=_PROCESS_TERMINATE)

        def kill_workers(self):
            """Attempts to kill the executor's workers.
            Iterates through all of the current worker processes and kills
            each one that is still alive.

            After killing workers, the pool will be in a broken state
            and no longer usable (for instance, new tasks should not be
            submitted).
            """
            return self._force_shutdown(operation=_PROCESS_KILL)


if _PY314_PLUS:
    from concurrent import interpreters
    from concurrent.futures.interpreter import BrokenInterpreterPool

    def _interpreter_do_call(results, func, args, kwargs):
        try:
            return func(*args, **kwargs)
        except BaseException as exc:
            # Send the captured exception out on the results queue, but still
            # leave it unhandled for the interpreter to handle.
            try:
                results.put(exc)
            except interpreters.NotShareableError:
                print("exception is not shareable:", file=sys.stderr)
                print(
                    "".join(format_exception(type(exc), exc, exc.__traceback__)),
                    file=sys.stderr,
                )
                results.put(None)
            raise

    class InterpreterWorkerContext(WorkerContext):
        """Worker context that runs each worker in a subinterpreter."""

        @classmethod
        def prepare(cls, initializer=None, initargs=()):
            def resolve_task(fn, args, kwargs):
                if isinstance(fn, str):
                    raise TypeError("scripts not supported")
                return (fn, args, kwargs)

            if initializer is not None:
                if not callable(initializer):
                    raise TypeError("initializer must be a callable")
                initdata = resolve_task(initializer, initargs, {})
            else:
                initdata = None

            def create_context():
                return cls(initdata)

            return create_context, resolve_task

        def __init__(self, initdata):
            self.initdata = initdata
            self.interp = None
            self.results = None

        def __del__(self):
            if self.interp is not None:
                self.finalize()

        def initialize(self):
            assert self.interp is None, self.interp
            self.interp = interpreters.create()
            try:
                self.results = interpreters.create_queue(0)
                if self.initdata:
                    self.run(self.initdata)
            except BaseException:
                self.finalize()
                raise

        def finalize(self):
            interp = self.interp
            results = self.results
            self.results = None
            self.interp = None
            if results is not None:
                del results
            if interp is not None:
                interp.close()

        def run(self, task):
            assert self.interp is not None
            assert self.results is not None
            try:
                return self.interp.call(_interpreter_do_call, self.results, *task)
            except interpreters.ExecutionFailed as wrapper:
                exc = self.results.get()
                if not isinstance(exc, BaseException):
                    raise
                raise exc from wrapper

    class InterpreterPoolExecutor(ThreadPoolExecutor):
        """A thread pool whose workers each run in a subinterpreter.

        This is only available on Python 3.14+.
        """

        BROKEN = BrokenInterpreterPool

        @classmethod
        def prepare_context(cls, initializer=None, initargs=()):
            return InterpreterWorkerContext.prepare(initializer, initargs)

        def __init__(
            self,
            max_workers=None,
            thread_name_prefix="",
            initializer=None,
            initargs=(),
            *,
            max_tasks_per_child=None,
            idle_timeout: float | None = 60.0,
        ):
            if max_tasks_per_child is not None:
                if not isinstance(max_tasks_per_child, int):
                    raise TypeError("max_tasks_per_child must be an integer")
                if max_tasks_per_child <= 0:
                    raise ValueError("max_tasks_per_child must be >= 1")

            if not thread_name_prefix:
                thread_name_prefix = f"InterpreterPoolExecutor-{self._counter()}"

            super().__init__(
                max_workers,
                thread_name_prefix,
                initializer,
                initargs,
                idle_timeout,
            )
            self._max_tasks_per_child = max_tasks_per_child


class AsyncWorkerContext:
    """Worker context for async pool executor.

    This class provides a way to customize async worker initialization,
    task execution, and cleanup. It supports both sync and async initializers.

    Example:
        class DatabaseContext(AsyncWorkerContext):
            async def initialize(self):
                self.db = await create_async_db_connection()

            async def finalize(self):
                await self.db.close()

            async def run(self, task):
                fn, args, kwargs = task
                return await fn(self.db, *args, **kwargs)
    """

    @classmethod
    def prepare(
        cls, initializer: "Callable | None" = None, initargs: tuple = ()
    ) -> tuple["Callable[[], AsyncWorkerContext]", "Callable[..., tuple]"]:
        """Prepare context factory and task resolver.

        :type initializer: callable, optional
        :param initializer: A callable used to initialize worker threads.
            Can be sync or async.

        :type initargs: tuple, optional
        :param initargs: A tuple of arguments to pass to the initializer.

        :return: A tuple of (create_context, resolve_task) functions.
        :rtype: tuple
        """
        if initializer is not None:
            if not callable(initializer):
                raise TypeError("initializer must be a callable")

        def create_context() -> "AsyncWorkerContext":
            return cls(initializer, initargs)

        def resolve_task(fn, args, kwargs) -> tuple:
            return (fn, args, kwargs)

        return create_context, resolve_task

    def __init__(self, initializer: "Callable | None" = None, initargs: tuple = ()):
        self.initializer = initializer
        self.initargs = initargs

    async def initialize(self) -> None:
        """Initialize the worker context.

        Called once when the worker thread starts and event loop is ready.
        """
        if self.initializer is not None:
            if iscoroutinefunction(self.initializer):
                await self.initializer(*self.initargs)
            else:
                self.initializer(*self.initargs)

    async def finalize(self) -> None:
        """Finalize the worker context.

        Called once when the worker thread exits.
        """

    async def run(self, task: tuple) -> "T":
        """Execute a task.

        :type task: tuple
        :param task: A tuple of (fn, args, kwargs).

        :return: The result of the task execution.
        :rtype: T
        """
        fn, args, kwargs = task
        if iscoroutinefunction(fn):
            return await fn(*args, **kwargs)
        return fn(*args, **kwargs)


class _AsyncWorkItem:
    """A work item for the async pool executor."""

    def __init__(self, future: Future, task: tuple):
        self.future = future
        self.task = task

    async def run(self, ctx: AsyncWorkerContext) -> None:
        if not self.future.set_running_or_notify_cancel():
            return

        try:
            result = await ctx.run(self.task)
        except BaseException as exc:
            self.future.set_exception(exc)
            # Break a reference cycle with the exception 'exc'
            del self
        else:
            self.future.set_result(result)

    __class_getitem__ = classmethod(types.GenericAlias)


async def _async_worker(
    executor_ref,
    ready: Event,
    max_workers: int,
    idle_timeout: float,
    create_context: "Callable[[], AsyncWorkerContext]",
):
    """Worker function for the async pool executor with idle timeout support."""
    if max_workers <= 0:
        raise ValueError(f"max_workers must be greater than 0, got {max_workers}")

    ctx = create_context()
    try:
        await ctx.initialize()
    except BaseException:
        _base.LOGGER.critical("Exception in initializer:", exc_info=True)
        executor = executor_ref()
        if executor is not None:
            executor._initializer_failed()
        return

    semaphore = asyncio.Semaphore(max_workers)
    concurrency = 0

    def on_task_done(_):
        nonlocal concurrency
        concurrency -= 1
        semaphore.release()

    loop = asyncio.get_running_loop()
    work_queue = AsyncQueue()
    if (executor := executor_ref()) is not None:
        executor._mark_thread_active(loop, work_queue)
    del executor

    async def run(tg: asyncio.TaskGroup):
        nonlocal concurrency

        ready.set()
        idle_tick = monotonic()

        try:
            while True:
                if await semaphore.acquire():
                    semaphore.release()
                try:
                    async with timeout(0.1):
                        work_item = await work_queue.get()
                    if work_item is None:
                        break

                    # Create task that runs the work item with context
                    async def run_with_context(item):
                        await item.run(ctx)

                    task = tg.create_task(run_with_context(work_item))
                    task.add_done_callback(on_task_done)
                    concurrency += 1
                    del work_item
                    idle_tick = monotonic()
                except (Empty, TimeoutError):
                    pass
                if concurrency > 0:
                    idle_tick = monotonic()
                elif idle_timeout >= 0 and monotonic() - idle_tick > idle_timeout:
                    break
        finally:
            if (executor := executor_ref()) is not None:
                executor._mark_thread_inactive()
                if _shutdown or executor._shutdown:
                    executor._shutdown = True
                    work_queue.put_nowait(None)
            del executor

    try:
        async with asyncio.TaskGroup() as tg:
            await run(tg)
    finally:
        await ctx.finalize()


class AsyncPoolExecutor(_base.Executor):
    """An async pool executor with automatic worker scaling and idle timeout.

    This executor runs coroutines in a dedicated worker thread with its own
    event loop. It supports concurrent execution of multiple coroutines with
    a semaphore-based limit.
    """

    _counter = itertools.count().__next__

    @classmethod
    def prepare_context(
        cls, initializer: "Callable | None" = None, initargs: tuple = ()
    ) -> tuple["Callable[[], AsyncWorkerContext]", "Callable[..., tuple]"]:
        """Prepare context factory and task resolver.

        This class method can be overridden to provide a custom AsyncWorkerContext.

        :type initializer: callable, optional
        :param initializer: A callable used to initialize worker threads.
            Can be sync or async.

        :type initargs: tuple, optional
        :param initargs: A tuple of arguments to pass to the initializer.

        :return: A tuple of (create_context, resolve_task) functions.
        :rtype: tuple
        """
        return AsyncWorkerContext.prepare(initializer, initargs)

    def __init__(
        self,
        max_workers: int | None = None,
        thread_name_prefix: str = "",
        initializer=None,
        initargs=(),
        idle_timeout: float | None = 60.0,
    ):
        """Initializes a new AsyncPoolExecutor instance.

        :type max_workers: int, optional
        :param max_workers: The maximum number of concurrent coroutines to run.
            Defaults to 262144.

        :type thread_name_prefix: str, optional
        :param thread_name_prefix: An optional name prefix to give our threads.

        :type initializer: callable, optional
        :param initializer: A callable used to initialize worker threads.
            Can be a sync function or an async coroutine function.

        :type initargs: tuple, optional
        :param initargs: A tuple of arguments to pass to the initializer.

        :type idle_timeout: float, optional
        :param idle_timeout: The maximum amount of time (in seconds) that a worker
            thread can remain idle before it is terminated. If set to None or negative
            value, workers will never be terminated. Defaults to 60 seconds.
        """
        super().__init__()
        if max_workers is None:
            max_workers = 262144
        if max_workers <= 0:
            raise ValueError("max_workers must be greater than 0")
        if not thread_name_prefix:
            thread_name_prefix = f"AsyncPoolExecutor-{self._counter()}"

        self._broken = False
        self._shutdown = False
        self._shutdown_lock = Lock()
        self._max_workers = max_workers
        self._thread_name_prefix = thread_name_prefix
        self._thread: Thread | None = None
        self._thread_active = Event()

        # Prepare context factory and task resolver
        (
            self._create_worker_context,
            self._resolve_work_item_task,
        ) = type(self).prepare_context(initializer, initargs)

        # Wrap (loop, queue) into a list to make they can be accessed in weakref
        # callback even if worker has been changed.
        self._comp: list[_AsyncComponent] = []

        if idle_timeout is None or idle_timeout < 0:
            self._idle_timeout = -1
        else:
            self._idle_timeout = max(0.1, idle_timeout)
        _async_executors.add(self)

    def submit(
        self,
        fn: "Callable[P, T]",
        /,
        *args: "P.args",
        **kwargs: "P.kwargs",
    ) -> "Future[T]":
        if not iscoroutinefunction(fn):
            raise TypeError("fn must be a coroutine function")

        with self._shutdown_lock, _global_shutdown_lock:
            if self._broken:
                raise BrokenThreadPool(self._broken)

            if self._shutdown:
                raise RuntimeError("cannot schedule new futures after shutdown")
            if _shutdown:
                raise RuntimeError(  # pragma: no cover
                    "cannot schedule new futures after interpreter shutdown"
                )

            self._start_worker_thread()
            if self._broken:
                raise BrokenThreadPool(self._broken)
            assert self._comp

            f = Future()
            task = self._resolve_work_item_task(fn, args, kwargs)
            w = _AsyncWorkItem(f, task)
            loop, work_queue = self._comp[0]
            loop.call_soon_threadsafe(work_queue.put_nowait, w)

            return f

    submit.__doc__ = _base.Executor.submit.__doc__

    def _start_worker_thread(self):
        if self._thread_active.is_set():
            return

        def weakref_cb(_, c: list[_AsyncComponent] = self._comp):
            if not c:
                return
            loop, work_queue = c[0]
            loop.call_soon_threadsafe(work_queue.put_nowait, None)

        ready = Event()

        def run_worker():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(
                    _async_worker(
                        ref(self, weakref_cb),
                        ready,
                        self._max_workers,
                        self._idle_timeout,
                        self._create_worker_context,
                    )
                )
            finally:
                loop.close()

        t = Thread(
            name=f"{self._thread_name_prefix or self}_0",
            target=run_worker,
        )
        t.start()
        self._thread = t

        # Check if initializer has finished or failed
        while not self._broken:
            if ready.is_set():
                break
            ready.wait(0.1)

    def _mark_thread_active(self, loop: AbstractEventLoop, work_queue: AsyncQueue):
        self._comp.append((loop, work_queue))
        self._thread_active.set()

    def _mark_thread_inactive(self):
        self._comp.clear()
        self._thread_active.clear()

    def _cancel_pending_futures(self, loop: AbstractEventLoop, work_queue: AsyncQueue):
        async def drain_and_cancel():
            cancelled = []
            while True:
                try:
                    work_item = work_queue.get_nowait()
                    if work_item is not None:
                        cancelled.append(work_item)
                except asyncio.QueueEmpty:
                    break
            return cancelled

        future = asyncio.run_coroutine_threadsafe(drain_and_cancel(), loop)
        try:
            work_items = future.result(timeout=5.0)
            for work_item in work_items:
                work_item.future.set_exception(BrokenThreadPool(self._broken))
        except Exception:
            pass

    def _initializer_failed(self):
        self._broken = True
        self._mark_thread_inactive()
        if c := self._comp:
            loop, work_queue = c[0]
            c.clear()
            self._cancel_pending_futures(loop, work_queue)

    def shutdown(self, wait=True, *, cancel_futures=False):
        with self._shutdown_lock:
            self._shutdown = True

            if c := self._comp:
                loop, work_queue = c[0]
                c.clear()

                if cancel_futures:
                    self._cancel_pending_futures(loop, work_queue)

                loop.call_soon_threadsafe(work_queue.put_nowait, None)

        if wait and (t := self._thread) is not None:
            t.join()
            self._thread = None
        self._mark_thread_inactive()

    shutdown.__doc__ = _base.Executor.shutdown.__doc__
