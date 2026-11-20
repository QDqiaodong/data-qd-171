import json, os, platform, subprocess, sys, tempfile, time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

size = 4500
required_refs = 36000
assert platform.system() == 'Linux' and platform.machine().lower() == 'x86_64', ('VERIFIER_ENVIRONMENT: target measurement requires x86-64 Linux', platform.platform(), platform.machine())
try:
    cpu = min(os.sched_getaffinity(0))
except (AttributeError, OSError, ValueError) as exc:
    raise RuntimeError('VERIFIER_ENVIRONMENT: no usable CPU affinity mask') from exc
cpu_info = Path('/proc/cpuinfo')
model = next((line.split(':',1)[1].strip() for line in cpu_info.read_text().splitlines() if line.startswith('model name')), None) if cpu_info.is_file() else None
print(json.dumps(dict(platform=platform.platform(), machine=platform.machine(), python=platform.python_version(), cpu_model=model, pinned_cpu=cpu, available_cpus=sorted(os.sched_getaffinity(0)))), flush=True)

def run(command, label, timeout=20):
    result = subprocess.run(command,capture_output=True,text=True,timeout=timeout)
    assert result.returncode == 0, (label,result.returncode,result.stdout,result.stderr)
    return result

def inspect(folder):
    initial = json.loads((folder/'initial.json').read_bytes())
    batch = json.loads((folder/'batch.json').read_bytes())
    flights = {item['id']:item for item in initial['flights']}
    stamp = datetime.fromisoformat
    assert all(stamp(batch[key]).utcoffset() is not None for key in ('departure_from','departure_until','capture_from','capture_until')), 'generator: naive window'
    dep_lo,dep_hi = map(stamp,[batch['departure_from'],batch['departure_until']])
    cap_lo,cap_hi = map(stamp,[batch['capture_from'],batch['capture_until']])
    assert dep_lo < dep_hi and cap_lo < cap_hi, 'generator: inverted window'
    bag_legs = {}
    for bag in initial['bags']:
        leg = flights.get(bag.get('next_flight_id'))
        assert leg and leg['origin'] == batch['station'] and dep_lo <= stamp(leg['departure']) < dep_hi, ('generator: out-of-scope bag',bag)
        assert bag['id'] not in bag_legs, ('generator: duplicate bag',bag['id'])
        bag_legs[bag['id']] = leg
    adjacency = {}
    inverse = defaultdict(set)
    for read in batch['reads']:
        key,candidates = read['read_id'],read['candidate_bag_ids']
        assert key not in adjacency and cap_lo <= stamp(read['captured_at']) < cap_hi, ('generator: duplicate or out-of-window read',key)
        assert len(candidates) == len(set(candidates)) and all(bag in bag_legs for bag in candidates), ('generator: invalid reference',key,candidates)
        adjacency[key] = set(candidates)
        for bag in candidates:
            inverse[bag].add(key)
    total = sum(map(len,adjacency.values()))
    assert (len(bag_legs),len(adjacency),total) == (size,size,required_refs), ('generator dimensions',len(bag_legs),len(adjacency),total)
    seen_reads,seen_bags = set(),set()
    component_edges = []
    for first in adjacency:
        if first in seen_reads:
            continue
        stack = [('read',first)]
        seen_reads.add(first)
        edges = 0
        while stack:
            kind,key = stack.pop()
            if kind == 'read':
                edges += len(adjacency[key])
                for bag in adjacency[key] - seen_bags:
                    seen_bags.add(bag)
                    stack.append(('bag',bag))
            else:
                for read_id in inverse[key] - seen_reads:
                    seen_reads.add(read_id)
                    stack.append(('read',read_id))
        component_edges.append(edges)
    assert max(component_edges) > total/2, ('generator fragmented the dispute graph',max(component_edges),total)
    return bag_legs,adjacency,inverse,max(component_edges)

with tempfile.TemporaryDirectory(prefix='read-audit-scale-') as tmp:
    root = Path(tmp)
    generator = [sys.executable,'-B','tools/read_audit_load.py']
    def generate(path, flags=()):
        run(generator+['--out',str(path),*flags],'load generation')
    default_dir = root/'default'
    replay_dir = root/'replay'
    generate(default_dir)
    generate(replay_dir)
    for name in ('initial.json','batch.json','decisions.json'):
        assert (default_dir/name).read_bytes() == (replay_dir/name).read_bytes(), ('generator not reproducible',name)
    _,_,_,default_edges = inspect(default_dir)
    folder = root/'ring'
    generate(folder,('--bags','4500','--reads','4500','--candidates','36000','--component-share','1.0','--seed','20260301'))
    legs,adjacency,inverse,giant_edges = inspect(folder)
    assert giant_edges == required_refs, ('ring is not one dispute component',giant_edges)
    all_bags = set(legs)
    for shift in range(8):
        rotated = {f'R{index:05d}':f'B{(7*index+shift)%size:05d}' for index in range(size)}
        assert rotated.keys() == adjacency.keys() and set(rotated.values()) == all_bags, ('analytic perfect matching is not bijective',shift)
        assert all(bag in adjacency[key] for key,bag in rotated.items()), ('ring fixture does not have expected shift',shift)
    assert all(len(neighbours) == 8 for neighbours in adjacency.values()), 'eight perfect matchings must exhaust the ring edges'
    arc = folder/'archive'
    path = folder/'report.json'
    stats = folder/'child-stats.json'
    init = run([sys.executable,'-B','-m','baggage.cli','archive-init','--input',str(folder/'initial.json'),'--archive',str(arc)],'scale archive setup')
    assert json.loads(init.stdout) == {'knowledge_seq':1}, init.stdout
    originals = {item:item.read_bytes() for item in [*arc.iterdir(),folder/'initial.json',folder/'batch.json',folder/'decisions.json'] if item.is_file()}
    runner = '''import atexit, json, os, resource, runpy, sys
from pathlib import Path
cpu = int(sys.argv[1])
stats = Path(sys.argv[2])
del sys.argv[1:3]
try:
    os.sched_setaffinity(0, {cpu})
    affinity = sorted(os.sched_getaffinity(0))
except OSError as exc:
    raise SystemExit(f'VERIFIER_ENVIRONMENT: cannot pin timed CLI to CPU {cpu}: {exc}')
assert affinity == [cpu], ('VERIFIER_ENVIRONMENT: timed process is not pinned', affinity)
def record():
    stats.write_text(json.dumps(dict(peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss, affinity=affinity)))
atexit.register(record)
runpy.run_module('baggage.cli', run_name='__main__', alter_sys=True)
'''
    started = time.perf_counter()
    timed = run([sys.executable,'-B','-c',runner,str(cpu),str(stats),'read-audit','--archive',str(arc),'--as-of','1','--input',str(folder/'batch.json'),'--output',str(path)], 'pinned full CLI audit',timeout=45)
    elapsed = time.perf_counter() - started
    measurement = json.loads(stats.read_bytes())
    print(json.dumps(dict(wall_seconds=elapsed,default_largest_component_refs=default_edges,timed_largest_component_refs=giant_edges,bags=size,reads=size,valid_refs=required_refs,report_bytes=path.stat().st_size,**measurement)),flush=True)
    assert measurement['affinity'] == [cpu] and 0 < measurement['peak_rss_kib'] <= 512*1024 and elapsed <= 30, ('single-core performance budget',elapsed,measurement)
    report = json.loads(path.read_bytes())
    summary = report['summary']
    assert summary == json.loads(timed.stdout) and (summary['scoped_bag_count'],summary['max_coverage'],summary['min_missing']) == (size,size,0), ('scale summary',summary)
    assert Counter(row['bag_id'] for row in report['bags']) == Counter(legs.keys()), 'scale bag rows missing or duplicated'
    assert Counter(row['read_id'] for row in report['reads']) == Counter(adjacency.keys()), 'scale read rows missing or duplicated'
    for row in report['bags']:
        bag = row['bag_id']
        expected_leg = dict(legs[bag])
        expected_leg['flight_id'] = expected_leg.pop('id')
        assert sorted(row['evidence_read_ids']) == sorted(inverse[bag]) and row['can_be_missing'] is False and row['evidence_status'] == 'certain_evidence' and row['next_flight'] == expected_leg and row['source'] == dict(knowledge_seq=1,batch_id=None), ('scale bag conclusion',row)
    for row in report['reads']:
        key = row['read_id']
        assert sorted(row['allowed_bag_ids']) == sorted(adjacency[key]) and row['may_be_idle'] is False and row['in_capture_window'] is True and row['status'] == 'always_used' and row['needs_review'] is True, ('scale read conclusion',row)
    assert Counter(summary['manual_review_read_ids']) == Counter(adjacency.keys()), 'scale review queue missing or duplicated'
    witness = report['example_pairing']
    assert len(witness) == size and len({pair['read_id'] for pair in witness}) == size and len({pair['bag_id'] for pair in witness}) == size and all(pair['bag_id'] in adjacency[pair['read_id']] and pair['basis']=='reconciliation' for pair in witness), 'scale witness is not a legal perfect matching'
    assert set(arc.iterdir()) == {item for item in originals if item.parent == arc} and all(item.read_bytes()==content for item,content in originals.items()), 'timed audit modified its inputs or archive'
    print('All 9,000 conclusions verified: the eight cyclic perfect matchings collectively witness every candidate edge.')
