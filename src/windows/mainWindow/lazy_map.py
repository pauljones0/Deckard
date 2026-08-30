"""Run queued work in order when a widget first maps, then clear the queue.
An exception stops the drain and preserves remaining tasks; owners must connect on_map and initialize on_map_tasks."""
from collections.abc import Callable


class LazyMapTasks:
    on_map_tasks: list[Callable[[], object]]

    def on_map(self, *args: object) -> None:
        for task in self.on_map_tasks:
            task()
        self.on_map_tasks.clear()
