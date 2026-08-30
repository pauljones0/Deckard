"""Drain lazy on-map tasks in order and clear the queue after success.
A raising task must stop the drain and preserve the queue."""

import fixtures  # noqa: F401  (must be first: isolates DATA_PATH before globals)

from src.windows.mainWindow.lazy_map import LazyMapTasks


class _Widget(LazyMapTasks):
    """A bare owner of the queue, the way every real row wires it."""

    def __init__(self) -> None:
        self.on_map_tasks = []


def test_drain_runs_in_order_then_clears() -> None:
    widget = _Widget()
    order: list[int] = []
    widget.on_map_tasks.append(lambda: order.append(1))
    widget.on_map_tasks.append(lambda: order.append(2))
    widget.on_map_tasks.append(lambda: order.append(3))

    # The map signal passes the widget; the drain takes it through *args.
    widget.on_map(widget)

    assert order == [1, 2, 3], f"the drain ran the tasks out of order: {order}"
    assert widget.on_map_tasks == [], (
        f"the drain left the queue unclear: {widget.on_map_tasks}"
    )


def test_second_map_runs_nothing() -> None:
    widget = _Widget()
    runs: list[str] = []
    widget.on_map_tasks.append(lambda: runs.append("first-map"))

    widget.on_map(widget)
    widget.on_map(widget)

    assert runs == ["first-map"], (
        f"a cleared queue ran a task again on the second map: {runs}"
    )


def test_raising_task_aborts_and_keeps_queue() -> None:
    # A raising task stops later tasks, preserves the queue, and propagates its error.
    widget = _Widget()
    runs: list[str] = []

    def boom() -> None:
        runs.append("boom")
        raise RuntimeError("task failed")

    widget.on_map_tasks.append(boom)
    widget.on_map_tasks.append(lambda: runs.append("after"))

    raised = False
    try:
        widget.on_map(widget)
    except RuntimeError:
        raised = True

    assert raised, "the drain swallowed the task error"
    assert runs == ["boom"], f"the drain ran past a raising task: {runs}"
    assert len(widget.on_map_tasks) == 2, (
        "a drain that raised must not clear the queue"
    )


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_on_map_drain")
    test_drain_runs_in_order_then_clears()
    test_second_map_runs_nothing()
    test_raising_task_aborts_and_keeps_queue()
    print("PASS: scenario_on_map_drain")


if __name__ == "__main__":
    main()
