#!/usr/bin/env python3
"""Exercise the old sampler against Linux accounting, including nested reaping.

These are synthetic COUNTER TESTS, never application speedup results.
"""
import argparse
import json
import os
from pathlib import Path
import resource
import subprocess
import sys
import threading
import time

import psutil
from accounting import CpuAccounting
from compare import snapshot


def burn(seconds):
    deadline = time.thread_time() + seconds
    while time.thread_time() < deadline:
        pass


def worker(kind):
    if kind == 'threads':
        # Short-lived threads are included in process and cgroup CPU totals.
        for _ in range(120):
            t = threading.Thread(target=burn, args=(0.005,))
            t.start()
            t.join()
    elif kind == 'detached-child':
        child = os.fork()
        if child == 0:
            grandchild = os.fork()
            if grandchild == 0:
                burn(0.3)
                os._exit(0)
            os._exit(0)
        os.waitpid(child, 0)
        time.sleep(1.2)
    elif kind == 'nested-reaping':
        for _ in range(4):
            child = os.fork()
            if child == 0:
                grandchild = os.fork()
                if grandchild == 0:
                    burn(0.08)
                    os._exit(0)
                burn(0.04)
                os.waitpid(grandchild, 0)
                # Keep the intermediate parent's waited-child counters visible.
                time.sleep(0.15)
                os._exit(0)
            time.sleep(0.35)
            os.waitpid(child, 0)
    else:
        burn(0.6)
    own = resource.getrusage(resource.RUSAGE_SELF)
    children = resource.getrusage(resource.RUSAGE_CHILDREN)
    print(json.dumps({'rusage_seconds': own.ru_utime + own.ru_stime + children.ru_utime + children.ru_stime}), flush=True)
    time.sleep(0.15)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    reports = []
    for kind in ('single', 'threads', 'nested-reaping', 'detached-child'):
        with CpuAccounting() as counter:
            p = subprocess.Popen(counter.command([sys.executable, __file__, 'worker', kind]), stdout=subprocess.PIPE, text=True)
            root = psutil.Process(p.pid)
            previous = 0.0
            accumulated = 0.0
            samples = []
            while p.poll() is None:
                try:
                    values, _, _, _ = snapshot(root)
                except psutil.NoSuchProcess:
                    break
                total = sum(values.values())
                accumulated += max(0, total - previous)
                samples.append({'old_total': total, 'kernel_total': counter.read()})
                previous = total
                time.sleep(1 if kind == 'detached-child' else 0.01)
            stdout, _ = p.communicate()
            truth = json.loads(stdout)
            row = {'case': kind, **truth, 'cgroup_seconds': counter.read(),
                   'old_sampler_seconds': accumulated, 'samples': samples}
            # Compare independent kernel interfaces; startup outside exec's cgroup is tiny.
            if kind == 'detached-child':
                assert row['cgroup_seconds'] - row['old_sampler_seconds'] > 0.25, row
            else:
                assert abs(row['cgroup_seconds'] - truth['rusage_seconds']) < 0.08, row
            reports.append(row)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(reports, indent=2) + '\n')
    for row in reports:
        print({k: v for k, v in row.items() if k != 'samples'})


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == 'worker':
        worker(sys.argv[2])
    else:
        main()
