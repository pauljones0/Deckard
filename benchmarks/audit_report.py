#!/usr/bin/env python3
"""Validate and summarize a completed measurement audit, without speedup claims."""
import argparse
import json
import gzip
from pathlib import Path
import statistics


def load(path):
    if path.exists():
        return json.loads(path.read_text())
    with gzip.open(str(path) + '.gz', 'rt') as stream:
        return json.load(stream)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    args = parser.parse_args()
    root = args.directory
    metadata = load(root / 'common-actions/metadata.json')
    runs = load(root / 'common-actions/results.json')
    assert not metadata['instrumented'] and not metadata['slow_tick_seconds']
    assert metadata['trials'] == 3 and metadata['sample_seconds'] >= 30
    assert len(runs) == 18
    table = []
    for workload in ('monitoring', 'graphs'):
        row = {'workload': workload, 'apps': {}}
        for app in ('original', 'direct', 'rust'):
            trials = [r for r in runs if r['app'] == app and r['workload'] == workload]
            assert {r['trial'] for r in trials} == {1, 2, 3}
            for r in trials:
                assert r['cpu_accounting'].startswith('cgroup-v2')
                assert len(r['samples']) >= 30
                counts = [s['processes'] for s in r['samples']]
                assert set(counts) == ({9} if workload == 'graphs' and app != 'rust' else {1}), counts
                counters = [s['cpu_seconds'] for s in r['samples']]
                assert all(a <= b for a, b in zip(counters, counters[1:]))
            row['apps'][app] = {key: {'median': statistics.median(r[key] for r in trials),
                                    'range': [min(r[key] for r in trials), max(r[key] for r in trials)]}
                                for key in ('cpu_percent', 'pss_mib')}
        table.append(row)
    profile = []
    for r in load(root / 'graph-profile/results.json'):
        first = r['process_cpu_before']
        last = r['samples'][-1]['process_cpu']
        elapsed = r['samples'][-1]['elapsed']
        pid = str(r['application_pid'])
        app_cpu = 100 * (last[pid]['seconds'] - first[pid]['seconds']) / elapsed
        child_cpu = 100 * sum(v['seconds'] - first[p]['seconds'] for p, v in last.items()
                              if p != pid and p in first and first[p]['created'] == v['created']) / elapsed
        assert abs(app_cpu + child_cpu - r['cpu_percent']) < 0.5
        profile.append({'app': r['app'], 'cgroup_cpu_percent': r['cpu_percent'],
                        'application_cpu_percent': app_cpu, 'children_cpu_percent': child_cpu})
    sampling = []
    for app in ('original', 'direct'):
        data = load(root / f'graph-profile/{app}-graphs-1-threads.json')
        seen = set()
        last = {}
        values = []
        first_values = []
        gaps = []
        for r in data['cpu_readings']:
            if r['elapsed'] > 15:
                values.append(r['value'])
                if r['ident'] not in seen:
                    first_values.append(r['value'])
                if r['ident'] in last:
                    gaps.append(r['elapsed'] - last[r['ident']])
            seen.add(r['ident'])
            last[r['ident']] = r['elapsed']
        sampling.append({'app': app, 'readings': len(values), 'zero_readings': values.count(0),
                         'new_ident_first_readings': first_values,
                         'median_interval_per_ident_seconds': statistics.median(gaps)})
    report = {'scope': 'real applications, fake Plus, four CPU/four RAM actions, visible editor; '
                       'defaults retained; live input values and graph pixels are not identical',
              'timings': table, 'diagnostic_cpu_attribution': profile,
              'diagnostic_plugin_sampling': sampling}
    (root / 'summary.json').write_text(json.dumps(report, indent=2) + '\n')
    for row in table:
        print(row['workload'], {app: [round(v['cpu_percent']['median'], 3), round(v['pss_mib']['median'], 1)]
                                for app, v in row['apps'].items()})


if __name__ == '__main__':
    main()
