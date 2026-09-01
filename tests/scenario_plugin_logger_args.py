"""Verify plugin loggers forward positional and keyword formatting arguments.
Messages without arguments must keep literal braces unchanged."""
import fixtures  # noqa: F401  (must be first: isolates DATA_PATH before globals)

import os  # noqa: E402

import globals as gl  # noqa: F401, E402
from loguru import logger  # noqa: E402

from fixtures import start_watchdog  # noqa: E402
from src.backend.Logger import Logger, LoggerConfig, Loglevel  # noqa: E402


def main() -> int:
    start_watchdog(30, "plugin_logger_args")

    captured: list[str] = []
    sink_id = logger.add(lambda m: captured.append(m.record["message"]),
                         level=0, filter=lambda r: r["level"].name.startswith("PLG_"))

    logs_dir = os.path.join(gl.DATA_PATH, "plglogs")
    os.makedirs(logs_dir, exist_ok=True)
    cfg = LoggerConfig(name="PLG", log_file_path=os.path.join(logs_dir, "plg.log"),
                       base_log_level="PLG_INFO", rotation="1 day", retention=1,
                       compression="zip")
    plugin_log = Logger(cfg, [Loglevel(name="INFO", method_name="info", priority=20, color="<white>")])

    failures: list[str] = []
    try:
        plugin_log.info("x={}", 42)
        plugin_log.info("host={host}", host="10.0.0.2")
        plugin_log.info("nothing to format {here}")
    finally:
        logger.remove(sink_id)
        plugin_log.remove_sink()

    if "x=42" not in captured:
        failures.append(f"a positional format arg was not applied: {captured}")
    if "host=10.0.0.2" not in captured:
        failures.append(f"a keyword format arg was not applied: {captured}")
    if "nothing to format {here}" not in captured:
        failures.append(f"a plain message with braces was mangled: {captured}")

    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        return 1
    print("PASS: the plugin logger forwards formatting arguments and leaves a "
          "plain message literal")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
