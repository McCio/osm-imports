"""Weighted parallel DAG task runner. Zero external dependencies."""

from __future__ import annotations

import graphlib
import multiprocessing.pool
import threading
import time
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Literal


class Task(ABC):
    """Base class for a single unit of pipeline work.

    Subclasses must implement ``name`` and ``run()``.
    Override ``weight`` (class attribute), ``run_in_process``, ``skip_if()``,
    and ``dependencies()`` as needed.
    """

    weight: float = 1
    run_in_process: bool = False

    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    def label(self) -> str:
        return self.name.split(":")[0]

    def log_cached(self) -> None:
        print(f"  [{self.label}] {self.name}: cached")

    def skip_if(self) -> bool:
        """Return True to skip run() and use the cached result."""
        return False

    @abstractmethod
    def run(self) -> None: ...

    def dependencies(self) -> list[Task]:
        return []


@dataclass
class Result:
    name: str
    status: Literal["done", "skipped", "failed"]
    duration: float = 0.0
    error: BaseException | None = None


class _WeightSemaphore:
    """Counting semaphore that limits total concurrent weight."""

    def __init__(self, capacity: float) -> None:
        self._capacity = capacity
        self._used: float = 0.0
        self._cond = threading.Condition()

    def acquire(self, weight: float) -> None:
        with self._cond:
            while self._used > 0 and self._used + weight > self._capacity:
                self._cond.wait()
            self._used += weight

    def release(self, weight: float) -> None:
        with self._cond:
            self._used -= weight
            self._cond.notify_all()


class Pipeline:
    def __init__(self) -> None:
        self._tasks: dict[str, Task] = {}

    def add(self, task: Task) -> Pipeline:
        """Register task and recursively add its dependencies (deduplicated by name)."""
        if task.name in self._tasks:
            return self
        self._tasks[task.name] = task
        for dep in task.dependencies():
            self.add(dep)
        return self

    def run(self, max_weight: float = 4) -> dict[str, Result]:
        """Execute all registered tasks respecting dependencies and weight budget.

        Tasks with run_in_process=True execute in worker processes (bypasses GIL).
        Skipped tasks (skip_if() == True) bypass the semaphore entirely.
        Raises the first exception encountered; in-flight tasks are allowed to finish.
        """
        if not self._tasks:
            return {}

        dep_names = {n: {d.name for d in t.dependencies()} for n, t in self._tasks.items()}
        for name, deps in dep_names.items():
            missing = deps - self._tasks.keys()
            if missing:
                raise ValueError(f"{name!r} depends on unregistered tasks: {missing}")

        ts = graphlib.TopologicalSorter(dep_names)
        ts.prepare()

        dag_lock = threading.Condition()
        sem = _WeightSemaphore(max_weight)
        results: dict[str, Result] = {}
        errors: list[BaseException] = []
        stop = threading.Event()

        use_procs = any(t.run_in_process for t in self._tasks.values())
        mp_pool: multiprocessing.pool.Pool | None = (
            multiprocessing.pool.Pool(processes=max_weight) if use_procs else None
        )

        pool = ThreadPoolExecutor(max_workers=min(len(self._tasks), max(max_weight * 4, 16)))

        def _callback(name: str, weight: int, result: Result) -> None:
            with dag_lock:
                results[name] = result
                if result.status == "failed":
                    errors.append(result.error)  # type: ignore[arg-type]
                else:
                    ts.done(name)
                    if not errors:
                        for ready in ts.get_ready():
                            pool.submit(_execute, ready)
                dag_lock.notify_all()

        def _execute(name: str) -> None:
            task = self._tasks[name]
            t0 = time.monotonic()
            try:
                if task.skip_if():
                    task.log_cached()
                    _callback(name, 0, Result(name, "skipped", time.monotonic() - t0))
                    return
            except Exception as e:
                _callback(name, 0, Result(name, "failed", time.monotonic() - t0, e))
                return

            sem.acquire(task.weight)
            try:
                if task.run_in_process and mp_pool is not None:
                    # Poll with timeout so the thread can notice a stop signal quickly.
                    async_result = mp_pool.apply_async(task.run)
                    while True:
                        try:
                            async_result.get(timeout=0.1)
                            break
                        except multiprocessing.TimeoutError:
                            if stop.is_set():
                                raise RuntimeError("pipeline aborted")
                else:
                    task.run()
                _callback(name, task.weight, Result(name, "done", time.monotonic() - t0))
            except BaseException as e:
                _callback(name, task.weight, Result(name, "failed", time.monotonic() - t0, e))
            finally:
                sem.release(task.weight)

        try:
            with dag_lock:
                for name in ts.get_ready():
                    pool.submit(_execute, name)
                while ts.is_active() and not errors:
                    dag_lock.wait()
        except KeyboardInterrupt:
            stop.set()
            if mp_pool is not None:
                mp_pool.terminate()
            pool.shutdown(cancel_futures=True, wait=True)
            if mp_pool is not None:
                mp_pool.join()
                mp_pool = None
            raise
        else:
            pool.shutdown(wait=True)
            if mp_pool is not None:
                mp_pool.close()
                mp_pool.join()

        if errors:
            raise errors[0]
        return results


def run_dag(root_tasks: list[Task], max_weight: float) -> dict[str, Result]:
    """Build a pipeline from root tasks and run it, printing a summary."""
    pipeline = Pipeline()
    for task in root_tasks:
        pipeline.add(task)
    n = len(pipeline._tasks)
    print(f"\n=== DAG: {n} tasks, max_weight={max_weight} ===\n")
    results = pipeline.run(max_weight=max_weight)
    done = sum(1 for r in results.values() if r.status == "done")
    skipped = sum(1 for r in results.values() if r.status == "skipped")
    print(f"\nDone: {done} executed, {skipped} cached/skipped")
    return results
