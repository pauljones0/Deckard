"""Deferred work that runs when a widget first appears on screen.

A widget that is not mapped has no size and no frame yet, so a thumbnail load
or a first paint before the map wastes work or paints the wrong thing. The
owner queues that work in ``on_map_tasks`` while the widget stays hidden, and
this mixin drains the queue on the map signal.

The drain runs each task in the order it was queued and then clears the queue,
so a second map runs nothing. A task that raises stops the drain and leaves the
remaining tasks queued: the clear does not run, and the error reaches the
caller. Connect the map signal to ``self.on_map`` and set ``self.on_map_tasks``
to an empty list in the owner's ``__init__``.
"""
from collections.abc import Callable


class LazyMapTasks:
    on_map_tasks: list[Callable[[], object]]

    def on_map(self, *args: object) -> None:
        for task in self.on_map_tasks:
            task()
        self.on_map_tasks.clear()
