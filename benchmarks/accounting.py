"""Linux cgroup-v2 CPU accounting for benchmark children (developer tooling).

The kernel retains CPU usage of exited threads and descendants. Unlike polling
process trees, reaping or reparenting cannot lose or count that usage twice.
No fallback: a missing delegated cgroup makes a timing run fail explicitly.
"""
import os
from pathlib import Path
import sys
import time
import uuid


class CpuAccounting:
    method = "cgroup-v2 cpu.stat usage_usec; all threads and descendants, including exited tasks"

    def __init__(self, parent=None):
        if parent is None:
            relative = next(line[3:] for line in Path('/proc/self/cgroup').read_text().splitlines()
                            if line.startswith('0::'))
            # A sibling keeps the benchmark harness outside the measured group.
            parent = (Path('/sys/fs/cgroup') / relative.lstrip('/')).parent
        self.path = Path(parent) / f'deckard-bench-{os.getpid()}-{uuid.uuid4().hex[:12]}'
        try:
            self.path.mkdir()
            self.read()
        except OSError as error:
            if self.path.exists():
                self.path.rmdir()
            raise RuntimeError('CPU timing requires a writable delegated cgroup-v2 parent; '
                               'use --cgroup-parent with a delegated directory') from error

    def command(self, command):
        return [sys.executable, str(Path(__file__).resolve()), 'exec', str(self.path), *map(str, command)]

    def read(self):
        fields = dict(line.split() for line in (self.path / 'cpu.stat').read_text().splitlines())
        return int(fields['usage_usec']) / 1_000_000

    def processes(self):
        return [int(pid) for pid in (self.path / 'cgroup.procs').read_text().split()]

    def close(self):
        if not self.path.exists():
            return
        # This group contains only this benchmark trial; never user services.
        if (self.path / 'cgroup.procs').read_text().strip():
            (self.path / 'cgroup.kill').write_text('1')
        deadline = time.monotonic() + 5
        while True:
            try:
                self.path.rmdir()
                return
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.02)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


if __name__ == '__main__':
    if len(sys.argv) < 4 or sys.argv[1] != 'exec':
        raise SystemExit('usage: accounting.py exec CGROUP COMMAND [ARGS...]')
    (Path(sys.argv[2]) / 'cgroup.procs').write_text(str(os.getpid()))
    os.execvpe(sys.argv[3], sys.argv[3:], os.environ)
