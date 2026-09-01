"""Drain queued work in order on each map, then clear the queue.
An empty queue does nothing; an exception leaves the queue uncleared."""
from collections.abc import Callable


class LazyMapTasks:
    on_map_tasks: list[Callable[[], object]]

    def on_map(self, *args: object) -> None:
        for task in self.on_map_tasks:
            task()
        self.on_map_tasks.clear()
