import asyncio
import atexit
import itertools
from concurrent.futures import Future, ProcessPoolExecutor, _base
from concurrent.futures.thread import BrokenThreadPool, _WorkItem
from concurrent.futures.thread import ThreadPoolExecutor as _ThreadPoolExecutor
from inspect import iscoroutinefunction
from queue import Empty
from threading import Event, Lock, Thread
from time import monotonic
from weakref import WeakKeyDictionary, ref

__all__ = (
    "__version__",
    "AsyncPoolExecutor",
    "BrokenThreadPool",
    "Future",
    "ProcessPoolExecutor",
    "ThreadPoolExecutor",
)

__version__ = "0.0.9"

_threads_queues = WeakKeyDictionary()  # type: ignore
_shutdown = False
_global_shutdown_lock = Lock()


def _python_exit():
    global _shutdown
    with _global_shutdown_lock:
        _shutdown = True
    items = list(_threads_queues.items())
    for t, q in items:
        q.put(None)
    for t, q in items:
        t.join()


atexit.register(_python_exit)


def _worker(executor_ref, work_queue, initializer, initargs, idle_timeout):
    if initializer is not None:
        try:
            initializer(*initargs)
        except BaseException:
            _base.LOGGER.critical("Exception in initializer:", exc_info=True)
            executor = executor_ref()
            if executor is not None:  # pragma: no cover
                executor._initializer_failed()
            return

    idle_tick = -1.0
    try:
        while True:
            if idle_tick == -1.0:
                idle_tick = monotonic()
            elif idle_timeout >= 0 and monotonic() - idle_tick > idle_timeout:
                break
            try:
                work_item = work_queue.get(block=True, timeout=0.1)
            except Empty:
                continue
            if work_item is not None:
                work_item.run()
                del work_item

                executor = executor_ref()
                if executor is not None:
                    executor._idle_semaphore.release()
                del executor
                idle_tick = monotonic()
                continue
            break  # pragma: no cover
    finally:
        executor = executor_ref()
        if executor is None:
            work_queue.put(None)
        else:
            executor._idle_semaphore.acquire(timeout=0)
            if _shutdown or executor._shutdown:
                executor._shutdown = True
                work_queue.put(None)
        del executor


class ThreadPoolExecutor(_ThreadPoolExecutor):
    def __init__(
        self,
        max_workers=1024,
        thread_name_prefix="",
        initializer=None,
        initargs=(),
        idle_timeout=60.0,
    ):
        """Initializes a new ThreadPoolExecutor instance.

        :type max_workers: int, optional
        :param max_workers: The maximum number of workers to create. Defaults to 1024.

        :type thread_name_prefix: str, optional
        :param thread_name_prefix: An optional name prefix to give our threads.

        :type initializer: callable, optional
        :param initializer: A callable used to initialize worker threads.

        :type initargs: tuple, optional
        :parm initargs: A tuple of arguments to pass to the initializer.

        :type idle_timeout: float, optional
        :param idle_timeout: The maximum amount of time (in seconds) that a worker
            thread can remain idle before it is terminated. If set to None or negative
            value, workers will never be terminated. Defaults to 60 seconds.
        """
        if max_workers is None:
            max_workers = 1024
        super().__init__(max_workers, thread_name_prefix, initializer, initargs)
        if idle_timeout is None or idle_timeout < 0:
            self._idle_timeout = -1
        else:
            self._idle_timeout = max(0.1, idle_timeout)

    def submit(self, fn, /, *args, **kwargs):
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

            f = Future()  # type: ignore
            w = _WorkItem(f, fn, args, kwargs)

            self._work_queue.put(w)
            self._adjust_thread_count()
            return f

    submit.__doc__ = _base.Executor.submit.__doc__

    def _adjust_thread_count(self):
        if self._idle_semaphore.acquire(timeout=0):
            return
        threads = self._threads
        dead_threads = [t for t in threads if not t.is_alive()]
        for t in dead_threads:
            threads.remove(t)  # type: ignore

        def weakref_cb(_, q=self._work_queue):
            q.put(None)

        num_threads = len(threads)
        if num_threads < self._max_workers:
            t = Thread(
                name=f"{self._thread_name_prefix or self}_{num_threads}",
                target=_worker,
                args=(
                    ref(self, weakref_cb),
                    self._work_queue,
                    self._initializer,
                    self._initargs,
                    self._idle_timeout,
                ),
            )
            t.start()
            threads.add(t)  # type: ignore
            _threads_queues[t] = self._work_queue


class _AsyncWorkItem(_WorkItem):
    async def run(self):
        if not self.future.set_running_or_notify_cancel():
            print("cancelled")
            return

        try:
            result = await self.fn(*self.args, **self.kwargs)
            self.future.set_result(result)
        except BaseException as exc:
            self.future.set_exception(exc)
        finally:
            del self


async def _async_worker(
    executor_ref,
    work_queue,
    initializer,
    initargs,
    max_workers,
    idle_timeout,
):
    executor = executor_ref()
    if executor is not None:
        executor._running.set()

    if initializer is not None:
        try:
            initializer(*initargs)
        except BaseException:
            _base.LOGGER.critical("Exception in initializer:", exc_info=True)
            if executor is not None:  # pragma: no cover
                executor._running.set()
                executor._initializer_failed()
            return

    idle_tick = monotonic()
    curr_tasks = set()
    loop = asyncio.get_running_loop()
    asleep = asyncio.sleep

    try:
        while True:
            if idle_timeout >= 0 and monotonic() - idle_tick > idle_timeout:
                break
            if len(curr_tasks) < max_workers:
                try:
                    work_item = work_queue.get(block=True, timeout=0.1)
                    if work_item is not None:
                        task = loop.create_task(work_item.run())
                        curr_tasks.add(task)
                        await asleep(0)
                        del work_item
                    else:
                        break
                except Empty:
                    pass
            await asleep(0)
            finished_tasks = [t for t in curr_tasks if t.done()]
            for t in finished_tasks:
                curr_tasks.remove(t)
            if curr_tasks:
                idle_tick = monotonic()
    finally:
        while curr_tasks:
            await asleep(0)
            finished_tasks = [t for t in curr_tasks if t.done()]
            for t in finished_tasks:
                curr_tasks.remove(t)
        executor = executor_ref()
        if executor is None:
            work_queue.put(None)
        else:
            executor._running.clear()
            if _shutdown or executor._shutdown:
                executor._shutdown = True
                work_queue.put(None)
        del executor


class AsyncWorker(Thread):
    def __init__(
        self,
        name,
        executor_ref,
        work_queue,
        initializer,
        initargs,
        max_workers,
        idle_timeout,
    ):
        super().__init__(name=name)
        self._executor_ref = executor_ref
        self._work_queue = work_queue
        self._initializer = initializer
        self._initargs = initargs
        self._max_workers = max_workers
        self._idle_timeout = idle_timeout

    def run(self):
        loop = asyncio.new_event_loop()
        loop.run_until_complete(
            _async_worker(
                self._executor_ref,
                self._work_queue,
                self._initializer,
                self._initargs,
                self._max_workers,
                self._idle_timeout,
            )
        )


class AsyncPoolExecutor(ThreadPoolExecutor):
    _counter = itertools.count().__next__

    def __init__(
        self,
        max_workers=None,
        thread_name_prefix="",
        initializer=None,
        initargs=(),
        idle_timeout=60.0,
    ):
        """Initializes a new AsyncPoolExecutor instance.

        :type max_workers: int, optional
        :param max_workers: The maximum number of workers to create. Defaults to 261244.

        :type thread_name_prefix: str, optional
        :param thread_name_prefix: An optional name prefix to give our threads.

        :type initializer: callable, optional
        :param initializer: A callable used to initialize worker threads.

        :type initargs: tuple, optional
        :parm initargs: A tuple of arguments to pass to the initializer.

        :type idle_timeout: float, optional
        :param idle_timeout: The maximum amount of time (in seconds) that a worker
            thread can remain idle before it is terminated. If set to None or negative
            value, workers will never be terminated. Defaults to 60 seconds.
        """
        if max_workers is None:
            max_workers = 262144
        if not thread_name_prefix:
            thread_name_prefix = f"AsyncPoolExecutor-{self._counter()}"  # type: ignore
        super().__init__(max_workers, thread_name_prefix, initializer, initargs)
        del self._idle_semaphore
        self._running = Event()
        if idle_timeout is None or idle_timeout < 0:
            self._idle_timeout = -1
        else:
            self._idle_timeout = max(0.1, idle_timeout)

    def submit(self, fn, /, *args, **kwargs):
        if not iscoroutinefunction(fn):
            raise TypeError("fn must be a coroutine function")
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

            f = Future()  # type: ignore
            w = _AsyncWorkItem(f, fn, args, kwargs)

            self._work_queue.put(w)
            self._adjust_thread_count()
            return f

    submit.__doc__ = _base.Executor.submit.__doc__

    def _adjust_thread_count(self):
        if self._running.is_set():
            return
        threads = self._threads
        threads.clear()  # type: ignore

        def weakref_cb(_, q=self._work_queue):
            q.put(None)

        w = AsyncWorker(
            f"{self._thread_name_prefix or self}_0",
            ref(self, weakref_cb),
            self._work_queue,
            self._initializer,
            self._initargs,
            self._max_workers,
            self._idle_timeout,
        )
        w.start()
        threads.add(w)  # type: ignore
        _threads_queues[w] = self._work_queue
