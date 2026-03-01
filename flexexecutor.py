"""
Flexexecutor provides executors that can automatically scale the number of
workers up and down.

Copyright (c) 2020-2026, Leavers.
License: MIT
"""

import asyncio
import atexit
import itertools
import queue
import types
from asyncio import AbstractEventLoop
from asyncio import Queue as AsyncQueue
from asyncio.timeouts import timeout
from concurrent.futures import Future, ProcessPoolExecutor, _base
from concurrent.futures.thread import BrokenThreadPool
from inspect import iscoroutinefunction
from queue import Empty
from threading import Event, Lock, Semaphore, Thread
from time import monotonic
from typing import TYPE_CHECKING
from weakref import WeakKeyDictionary, ref, WeakSet

if TYPE_CHECKING:
    from typing_extensions import Callable, ParamSpec, TypeVar

    P = ParamSpec("P")
    T = TypeVar("T")

__all__ = (
    "__version__",
    "AsyncPoolExecutor",
    "AsyncWorkerContext",
    "BrokenThreadPool",
    "Future",
    "ProcessPoolExecutor",
    "ThreadPoolExecutor",
    "WorkerContext",
)

__version__ = "0.0.12"

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
    for t, q in threads_items:
        q.put(None)
    for t, q in threads_items:
        t.join()

    for e in _async_executors:
        e.shutdown()


atexit.register(_python_exit)


class WorkerContext:
    """Worker context for thread pool executor.

    This class provides a way to customize worker thread initialization,
    task execution, and cleanup. It is designed to be compatible with
    Python 3.10+ and follows the design of Python 3.14's ThreadPoolExecutor.

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

        Args:
            initializer: A callable used to initialize worker threads.
            initargs: A tuple of arguments to pass to the initializer.

        Returns:
            A tuple of (create_context, resolve_task) functions.
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

        Args:
            task: A tuple of (fn, args, kwargs).

        Returns:
            The result of the task execution.
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
            self = None  # noqa: F841
        else:
            self.future.set_result(result)

    __class_getitem__ = classmethod(types.GenericAlias)


def _worker(
    executor_ref,
    work_queue: queue.SimpleQueue,
    create_context: "Callable[[], WorkerContext]",
    idle_timeout: float,
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
    finally:
        ctx.finalize()
        executor = executor_ref()
        if executor is None:
            work_queue.put(None)
        else:
            executor._idle_semaphore.acquire(timeout=0)
            if _shutdown or executor._shutdown:
                executor._shutdown = True
                work_queue.put(None)
        del executor


class ThreadPoolExecutor(_base.Executor):
    """A thread pool executor with automatic worker scaling and idle timeout.

    This implementation is compatible with Python 3.11-3.14 and does not depend
    on internal CPython APIs.
    """

    _counter = itertools.count().__next__

    @classmethod
    def prepare_context(
        cls, initializer: "Callable | None" = None, initargs: tuple = ()
    ) -> tuple["Callable[[], WorkerContext]", "Callable[..., tuple]"]:
        """Prepare context factory and task resolver.

        This class method can be overridden to provide a custom WorkerContext.

        Args:
            initializer: A callable used to initialize worker threads.
            initargs: A tuple of arguments to pass to the initializer.

        Returns:
            A tuple of (create_context, resolve_task) functions.
        """
        return WorkerContext.prepare(initializer, initargs)

    def __init__(
        self,
        max_workers: int | None = 1024,
        thread_name_prefix: str = "",
        initializer=None,
        initargs=(),
        idle_timeout: float | None = 60.0,
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
        """
        if max_workers is None:
            max_workers = 1024
        if max_workers <= 0:
            raise ValueError("max_workers must be greater than 0")

        self._max_workers = max_workers
        self._work_queue: queue.SimpleQueue[_WorkItem | None] = queue.SimpleQueue()
        self._idle_semaphore = Semaphore(0)
        self._threads: set[Thread] = set()
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
        ) = type(self).prepare_context(initializer, initargs)

        if idle_timeout is None or idle_timeout < 0:
            self._idle_timeout = -1.0
        else:
            self._idle_timeout = max(0.1, idle_timeout)

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
                raise BrokenThreadPool(self._broken)

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

    def _adjust_thread_count(self):
        # If idle threads are available, don't spin new threads
        if self._idle_semaphore.acquire(timeout=0):
            return

        # Remove dead threads
        threads = self._threads
        dead_threads = [t for t in threads if not t.is_alive()]
        for t in dead_threads:
            threads.discard(t)

        # When the executor gets lost, the weakref callback will wake up
        # the worker threads.
        def weakref_cb(_, q=self._work_queue):
            q.put(None)

        num_threads = len(threads)
        if num_threads < self._max_workers:
            t = Thread(
                name=f"{self._thread_name_prefix}_{num_threads}",
                target=_worker,
                args=(
                    ref(self, weakref_cb),
                    self._work_queue,
                    self._create_worker_context,
                    self._idle_timeout,
                ),
            )
            t.start()
            threads.add(t)
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
                    work_item.future.set_exception(BrokenThreadPool(self._broken))

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
            for t in self._threads:
                t.join()

    shutdown.__doc__ = _base.Executor.shutdown.__doc__


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

        Args:
            initializer: A callable used to initialize worker threads.
                Can be sync or async.
            initargs: A tuple of arguments to pass to the initializer.

        Returns:
            A tuple of (create_context, resolve_task) functions.
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

        Args:
            task: A tuple of (fn, args, kwargs).

        Returns:
            The result of the task execution.
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
            self = None  # noqa: F841
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

        Args:
            initializer: A callable used to initialize worker threads.
                Can be sync or async.
            initargs: A tuple of arguments to pass to the initializer.

        Returns:
            A tuple of (create_context, resolve_task) functions.
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

            f = Future()  # type: ignore
            task = self._resolve_work_item_task(fn, args, kwargs)
            w = _AsyncWorkItem(f, task)
            loop, work_queue = self._comp[0]
            loop.call_soon_threadsafe(work_queue.put_nowait, w)

            return f

    submit.__doc__ = _base.Executor.submit.__doc__

    def _start_worker_thread(self):
        if self._thread_active.is_set():
            return

        def weakref_cb(_, c=self._comp):
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
