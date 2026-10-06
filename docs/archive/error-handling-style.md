# Error-handling house style

How this codebase catches, logs, and swallows exceptions. The lint gate
(`ruff check .`) enforces the two mechanical rules below; the rest is a
convention for reviewers and authors.

## Never a bare `except:`

A bare `except:` catches `BaseException`, so it also swallows
`KeyboardInterrupt`, `SystemExit`, and a pool worker's `CancelledError`. That
turns Ctrl-C into a no-op and hides a shutdown request. `ruff` rule `E722`
blocks the bare form.

Catch `Exception` at the widest. A pool worker or a thread target that must
stay alive on any fault still catches `Exception`, not `BaseException`, so the
interpreter's own exit signals pass through:

```python
try:
    result = self.get_remote_file(url, path, branch, data_type="content")
except Exception as e:
    # Catch Exception, so a pool worker still honours SystemExit and
    # KeyboardInterrupt.
    log.error(f"Failed to fetch image {path} from {url}: {e}")
    return None
```

Catch `BaseException` only where the intent is exactly "run this cleanup even
on interrupt, then re-raise":

```python
except BaseException:
    with contextlib.suppress(OSError):
        os.remove(tmp_path)
    raise
```

## Narrow the catch to the failure you expect

An expected, recoverable condition gets a narrow `except <SpecificError>`, not
an `Exception` catch that also hides a programming error such as a typo in a
method name. Narrow first; widen only where the failure set is genuinely open.

## A deliberate swallow is `contextlib.suppress`

A `try/except <Error>: pass` that ignores a failure on purpose reads as
`contextlib.suppress(<Error>)`. The context manager names the ignored error
class and states the intent in one line. `ruff` rule `SIM105` blocks the
single-statement `try/except/pass` form.

```python
# was: try / self.tasks.remove(task) / except ValueError / pass
with contextlib.suppress(ValueError):
    self.tasks.remove(task)
```

Suppress only where the failure truly carries no information: a best-effort
temp-file cleanup, a list removal of an item that may already be gone, a socket
or file descriptor close on teardown, a process reap that may race the exit. A
comment above the `with` says why the failure is safe when the reason is not
obvious from the call.

Do not reach for `contextlib.suppress` to quiet a failure that a reader should
see. A swallow that hides a real fault gets a log line or a handler instead:

```python
except AttributeError as e:
    log.error(e)
```

## Log through loguru, never `print`

Route a caught error through `loguru`, not `print`. `log.opt(exception=True)`
(or `log.exception(...)`) attaches the traceback, and the central exception
hooks and the log redaction both cover that path. A `print` on an error skips
redaction and the crash log.

The one exception is the crash-logging path itself. When `loguru` is the thing
that failed, the hooks fall back to `sys.__stderr__` and then swallow, because
one lost traceback costs less than a crash inside the crash handler.

## `@log.catch` at the top of a callback or thread

Put `@log.catch` on a top-level entry point that must "log and keep the app
alive": a GTK callback, a thread target, an event handler. It catches, logs
with a traceback, and returns. Inside such a body, a narrow `try/except` still
handles the conditions you expect and recover from; `@log.catch` is the net for
the ones you did not.
