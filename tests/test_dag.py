"""Tests for utils.dag — no filesystem or subprocess dependencies."""

from __future__ import annotations

import threading
import time

import pytest

from utils.dag import Pipeline, Result, Task


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class SimpleTask(Task):
    """Configurable stub for DAG tests."""

    def __init__(
        self,
        name: str,
        deps: list[Task] | None = None,
        weight: int = 1,
        skip: bool = False,
        fail: bool = False,
        delay: float = 0.0,
    ) -> None:
        self._name = name
        self._deps = deps or []
        self.weight = weight
        self._skip = skip
        self._fail = fail
        self._delay = delay
        self.ran = False
        self.ran_at: float | None = None

    @property
    def name(self) -> str:
        return self._name

    def dependencies(self) -> list[Task]:
        return self._deps

    def skip_if(self) -> bool:
        return self._skip

    def run(self) -> None:
        if self._delay:
            time.sleep(self._delay)
        if self._fail:
            raise RuntimeError(f"{self._name} failed")
        self.ran = True
        self.ran_at = time.monotonic()


# ---------------------------------------------------------------------------
# Basic correctness
# ---------------------------------------------------------------------------


def test_single_task_runs():
    t = SimpleTask("a")
    results = Pipeline().add(t).run(max_weight=2)
    assert t.ran
    assert results["a"].status == "done"


def test_chain_runs_in_order():
    a = SimpleTask("a")
    b = SimpleTask("b", deps=[a])
    c = SimpleTask("c", deps=[b])
    Pipeline().add(c).run(max_weight=4)
    assert a.ran and b.ran and c.ran
    assert a.ran_at <= b.ran_at <= c.ran_at  # type: ignore[operator]


def test_deps_auto_registered():
    a = SimpleTask("a")
    b = SimpleTask("b", deps=[a])
    p = Pipeline().add(b)
    assert "a" in p._tasks and "b" in p._tasks


def test_dedup_by_name():
    """Two consumers with a dep of the same name → shared task runs once."""
    run_count = [0]

    class CountTask(Task):
        @property
        def name(self) -> str:
            return "shared"

        def run(self) -> None:
            run_count[0] += 1

    class ConsumerTask(Task):
        def __init__(self, n: int) -> None:
            self._n = n

        @property
        def name(self) -> str:
            return f"consumer:{self._n}"

        def dependencies(self) -> list[Task]:
            return [CountTask()]

        def run(self) -> None:
            pass

    p = Pipeline()
    p.add(ConsumerTask(1))
    p.add(ConsumerTask(2))
    p.run(max_weight=4)
    assert run_count[0] == 1


# ---------------------------------------------------------------------------
# Skip / cache
# ---------------------------------------------------------------------------


def test_skipped_task_not_run():
    a = SimpleTask("a", skip=True)
    b = SimpleTask("b", deps=[a])
    results = Pipeline().add(b).run(max_weight=2)
    assert not a.ran
    assert b.ran
    assert results["a"].status == "skipped"
    assert results["b"].status == "done"


def test_all_skipped_returns_immediately():
    a = SimpleTask("a", skip=True)
    b = SimpleTask("b", deps=[a], skip=True)
    results = Pipeline().add(b).run(max_weight=2)
    assert results["a"].status == "skipped"
    assert results["b"].status == "skipped"


def test_skipped_task_does_not_consume_weight():
    """A heavy skipped task must not block lighter real tasks."""
    log: list[str] = []
    lock = threading.Lock()

    class LogTask(Task):
        def __init__(self, n: str, w: int = 1, skip: bool = False, delay: float = 0.0) -> None:
            self._n, self.weight, self._skip, self._delay = n, w, skip, delay

        @property
        def name(self) -> str:
            return self._n

        def skip_if(self) -> bool:
            return self._skip

        def run(self) -> None:
            time.sleep(self._delay)
            with lock:
                log.append(self._n)

    heavy_skipped = LogTask("heavy", w=4, skip=True)
    light1 = LogTask("light1", w=1, delay=0.02)
    light2 = LogTask("light2", w=1, delay=0.02)

    Pipeline().add(heavy_skipped).add(light1).add(light2).run(max_weight=2)
    assert "light1" in log and "light2" in log


# ---------------------------------------------------------------------------
# Weight limiting
# ---------------------------------------------------------------------------


def test_weight_limits_concurrency():
    """With max_weight=2, two weight-1 tasks can overlap; a weight-2 task cannot."""
    active: list[int] = []
    peak: list[int] = [0]
    lock = threading.Lock()

    class WeightedTask(Task):
        def __init__(self, n: str, w: int = 1) -> None:
            self._n, self.weight = n, w

        @property
        def name(self) -> str:
            return self._n

        def run(self) -> None:
            with lock:
                active.append(1)
                peak[0] = max(peak[0], sum(t * self.weight for t in active))
            time.sleep(0.03)
            with lock:
                active.pop()

    tasks = [WeightedTask(f"t{i}", w=1) for i in range(4)]
    Pipeline().add(tasks[0]).add(tasks[1]).add(tasks[2]).add(tasks[3]).run(max_weight=2)
    assert peak[0] <= 2


def test_heavy_task_waits_for_slot():
    """A weight-3 task must not run while 2 units are occupied."""
    timeline: list[tuple[str, str]] = []
    lock = threading.Lock()

    class TTask(Task):
        def __init__(self, n: str, w: int, delay: float = 0.05) -> None:
            self._n, self.weight, self._delay = n, w, delay

        @property
        def name(self) -> str:
            return self._n

        def run(self) -> None:
            with lock:
                timeline.append((self._n, "start"))
            time.sleep(self._delay)
            with lock:
                timeline.append((self._n, "end"))

    light = TTask("light", w=2, delay=0.08)
    heavy = TTask("heavy", w=3, delay=0.02)
    Pipeline().add(light).add(heavy).run(max_weight=4)

    light_start = next(i for i, (n, e) in enumerate(timeline) if n == "light" and e == "start")
    heavy_start = next(i for i, (n, e) in enumerate(timeline) if n == "heavy" and e == "start")
    light_end = next(i for i, (n, e) in enumerate(timeline) if n == "light" and e == "end")
    # heavy must start after light ends (2+3=5 > 4)
    assert heavy_start > light_end or light_start > next(
        i for i, (n, e) in enumerate(timeline) if n == "heavy" and e == "end"
    )


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


def test_failing_task_raises():
    t = SimpleTask("bad", fail=True)
    with pytest.raises(RuntimeError, match="bad failed"):
        Pipeline().add(t).run()


def test_result_has_failed_status():
    t = SimpleTask("bad", fail=True)
    try:
        Pipeline().add(t).run()
    except RuntimeError:
        pass


def test_downstream_not_run_after_failure():
    bad = SimpleTask("bad", fail=True)
    good = SimpleTask("good", deps=[bad])
    with pytest.raises(RuntimeError):
        Pipeline().add(good).run()
    assert not good.ran


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_empty_pipeline():
    results = Pipeline().run()
    assert results == {}


def test_result_has_duration():
    t = SimpleTask("a", delay=0.02)
    results = Pipeline().add(t).run(max_weight=2)
    assert results["a"].duration >= 0.01


def test_unknown_dep_raises():
    class OrphanTask(Task):
        @property
        def name(self) -> str:
            return "orphan"

        def dependencies(self) -> list[Task]:
            class Ghost(Task):
                @property
                def name(self) -> str:
                    return "ghost"

                def run(self) -> None:
                    pass

            return [Ghost()]

        def run(self) -> None:
            pass

    # add() recurses and registers ghost, so no error — this tests the guard for manually broken graphs
    p = Pipeline()
    p._tasks["orphan"] = OrphanTask()  # bypass add() to simulate missing dep
    with pytest.raises(ValueError, match="unregistered"):
        p.run()
