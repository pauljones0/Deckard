from dataclasses import dataclass
from typing import Any

from loguru import logger

# depth=1 reports the plugin caller without inspecting the full stack.
# This derived logger shares sinks and patchers with later configuration.
_CALLER_LOGGER = logger.opt(depth=1)

@dataclass
class Loglevel:
    name: str
    method_name: str
    priority: int
    color: str

@dataclass
class LoggerConfig:
    name: str

    log_file_path: str
    base_log_level: str
    rotation: str
    # Maximum rotated files to retain; without it, loguru keeps every rotation.
    retention: int
    compression: str

class Logger:
    def __init__(self, config: LoggerConfig, log_level: list[Loglevel]):
        self.name = config.name

        self.config = config
        self.log_level: dict[str, Loglevel] = {}
        self.sink_id: int | None = None

        for level in log_level:
            self.add_log_level(level)
            self.log_level[level.name] = level
        self.add_sink()

    def add_log_level(self, log_level: Loglevel) -> None:
        level_name = f"{self.name}_{log_level.name}"
        logger.level(
            name=level_name,
            no=log_level.priority,
            color=f"{log_level.color}")

        def log_method(self: Any, message: str, *args: Any, **kwargs: Any) -> None:
            # Loguru formats braces only when formatting arguments are present.
            # A plain message therefore keeps its literal braces.
            _CALLER_LOGGER.log(level_name, message, *args, **kwargs)

        setattr(self, log_level.method_name, log_method.__get__(self))

    def add_sink(self) -> None:
        # Build once because the filter checks every offered record.
        level_prefix = f"{self.config.name}_"

        def log_filter(record: Any) -> bool:
            return bool(record["level"].name.startswith(level_prefix))

        self.sink_id = logger.add(
            sink=self.config.log_file_path,
            level=self.config.base_log_level,
            rotation=self.config.rotation,
            retention=self.config.retention,
            compression=self.config.compression,
            enqueue=True,
            filter=log_filter,
            # {name} is the caller's module. For a plugin under the data
            # directory that is the dotted path plugins.<folder>.main.
            format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level} | {name} | {function}:{line} - {message}"
        )

    def remove_sink(self) -> None:
        """Detach the queued sink and release its POSIX semaphores.
        Call before os._exit(), which skips loguru cleanup."""
        if self.sink_id is not None:
            logger.remove(self.sink_id)
            self.sink_id = None

    def _log(self, level: str, message: str, *args: Any, **kwargs: Any) -> None:
        logger.log(level, message, *args, **kwargs)
