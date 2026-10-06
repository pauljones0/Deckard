#!/usr/bin/env python3
"""Instrument real upstream thread starts; diagnostic timings are not speedups.

Optional delay is a synthetic slow-plugin reproducer, not normal plugin behavior.
Application exports and their tick dispatch implementation remain untouched.
"""
import builtins
import collections
import json
from pathlib import Path
import runpy
import sys
import threading
import time
import psutil

source, output = map(lambda p: Path(p).resolve(), sys.argv[1:3])
delay = float(sys.argv[3])
sys.path.insert(0, str(source))
sys.argv = [str(source / 'main.py'), *sys.argv[4:]]
lock = threading.Lock()
starts = collections.Counter()
active = collections.Counter()
peaks = collections.Counter()
cpu_readings = []
base_cpu_percent = psutil.cpu_percent


def cpu_percent(*args, **kwargs):
    value = base_cpu_percent(*args, **kwargs)
    with lock:
        cpu_readings.append({'elapsed': time.monotonic() - started,
                             'thread': threading.current_thread().name,
                             'ident': threading.get_ident(), 'value': value})
    return value


psutil.cpu_percent = cpu_percent
base_start = threading.Thread.start


def start(self, *args, **kwargs):
    name = self.name
    original_run = self.run
    def run():
        with lock:
            active[name] += 1
            peaks[name] = max(peaks[name], active[name])
        try:
            return original_run()
        finally:
            with lock:
                active[name] -= 1
    self.run = run
    with lock:
        starts[name] += 1
    return base_start(self, *args, **kwargs)


threading.Thread.start = start
if delay:
    base_import = builtins.__import__
    patched = set()
    def intercept(name, *args, **kwargs):
        result = base_import(name, *args, **kwargs)
        for suffix in ('CPU.CPU', 'RAM.RAM'):
            module = sys.modules.get('plugins.com_core447_OSPlugin.actions.' + suffix)
            if module and module not in patched and hasattr(module, suffix.split('.')[-1]):
                cls = getattr(module, suffix.split('.')[-1])
                tick = cls.on_tick
                def slow(self, tick=tick):
                    time.sleep(delay)
                    return tick(self)
                cls.on_tick = slow
                patched.add(module)
        return result
    builtins.__import__ = intercept

started = time.monotonic()
samples = []
def publish():
    while True:
        time.sleep(1)
        with lock:
            samples.append({'elapsed': time.monotonic() - started, 'starts': dict(starts),
                            'active': dict(active), 'peaks': dict(peaks)})
            report = {'diagnostic_only': True, 'synthetic_tick_delay_seconds': delay,
                      'delay_classes_patched': len(patched) if delay else 0, 'samples': samples,
                      'cpu_readings': cpu_readings}
            output.write_text(json.dumps(report, indent=2) + '\n')

threading.Thread(target=publish, daemon=True, name='audit-publisher').start()
runpy.run_path(str(source / 'main.py'), run_name='__main__')
