import json,time
from ..config import ROOT,load
from .space import candidates
from .prune import reject
def prepare(path):
    start=time.monotonic();spec=load(path);base=load(ROOT/spec['base']);accepted=[];reasons={}
    for key,config in candidates(base,spec['variables'],spec['max_candidates']):
        reason=reject(config)
        if reason:reasons[reason]=reasons.get(reason,0)+1;continue
        directory=ROOT/'workspace/search/candidates';directory.mkdir(parents=True,exist_ok=True)
        target=directory/(key+'.yaml');target.write_text(json.dumps(config,indent=2)+'\n');accepted.append(str(target))
    summary={'accepted':accepted,'pruned':reasons,'elapsed_seconds':time.monotonic()-start,'evaluation_calls':0,'scope':'candidate preparation only; run explicitly after review'}
    (ROOT/'workspace/search/summary.json').write_text(json.dumps(summary,indent=2)+'\n')
    return summary


def execute(path, output, *, workers=None, resume=False):
    """先隔离构建，再按候选×案例动态评估；仅主控写入账本和看板。"""
    import fcntl
    import hashlib
    import re
    import tarfile
    from pathlib import Path
    from ..config import verify_official, digest, reference
    from ..evaluation.pipeline import runtime, task, record_result
    from ..evaluation.cache import engine_identity, canonical
    from ..records import read
    from .scheduler import run, atomic_json
    output = Path(output).resolve()
    if not output.is_relative_to(ROOT / 'workspace/search'):
        raise ValueError('搜索运行目录必须位于 workspace/search')
    existed = output.exists()
    if existed and not resume:
        raise FileExistsError(output)
    output.mkdir(parents=True, exist_ok=True)
    with (output / '.controller.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        started = time.monotonic()
        spec = load(path); base = load(ROOT / spec['base'])
        python, env = runtime()
        verify_official()
        source = {str(p.relative_to(ROOT)): digest(p) for p in sorted((ROOT / 'src').rglob('*.py'))}
        identity = {'source': source, 'engine': engine_identity(ROOT / 'vendor/official'),
                    'baseline_sha256': digest(ROOT / 'vendor/official/baseline_manifest.json'), 'spec': spec}
        identity_hash = hashlib.sha256(canonical(identity)).hexdigest()
        metadata = output / 'campaign.json'
        if metadata.exists():
            if load(metadata)['identity_sha256'] != identity_hash:
                raise ValueError('代码或搜索定义已改变，必须新建搜索批次')
        else:
            atomic_json(metadata, {'id': output.name, 'identity_sha256': identity_hash, 'identity': identity})
            with tarfile.open(output / 'source.tar.gz', 'w:gz') as archive:
                for name in source:
                    archive.add(ROOT / name, arcname=name)
        workers = workers or spec.get('workers', 8)
        fraction = spec.get('max_memory_fraction', .8)
        budget = spec.get('wall_budget_seconds', 7200)
        memory = int(spec.get('task_memory_mib', 1024)) * 1024**2
        seeds = spec.get('functional_seeds', [7])
        timeout = spec.get('task_timeout_seconds', 3600)
        if not seeds or not all(type(seed) is int for seed in seeds):
            raise ValueError('功能种子必须是非空整数列表')
        accepted = {}; pruned = {}; build_jobs = []
        anchor_key = 'anchor'
        accepted[anchor_key] = base
        for key, config in candidates(base, spec['variables'], spec['max_candidates']):
            reason = reject(config)
            if reason:
                pruned[reason] = pruned.get(reason, 0) + 1
            else:
                accepted[key] = config
        for key, config in accepted.items():
            cfg = output / 'configs' / (key + '.json')
            atomic_json(cfg, config)
            candidate = output / 'builds' / key
            if (candidate / 'build.json').exists():
                continue
            if candidate.exists():
                candidate.rename(candidate.with_name(candidate.name + '.incomplete-' + str(time.time_ns())))
            build_jobs.append({'key': key, 'command': [python, '-m', 'codesign_lab.cli', 'build',
                str(cfg), '--out', str(candidate)], 'cwd': str(ROOT), 'memory_bytes': memory})
        build_summary = run(build_jobs, output / 'build-jobs', workers=workers, memory_fraction=fraction,
                            timeout=min(timeout, 240), budget=budget, env=env)
        anchor = output / 'builds' / anchor_key
        if not (anchor / 'build.json').exists():
            raise RuntimeError('基准构建未成功')
        anchor_hashes = load(anchor / 'build.json')['sha256']
        jobs = []; details = {}; duplicates = 0; reused = 0; unchanged = 0; observations = []
        def observation(identifier, candidate, case, report, result):
            info = load(report).get('cases', {}).get(case, {}) if report.exists() else {}
            timing = info.get('timing', {})
            return {'record': identifier, 'case': case, 'status': result['status'],
                'functional_passed': info.get('functional_passed'), 'cycles': timing.get('cycles'),
                'peak_power_w': timing.get('peak_window_power_w'), 'wall_seconds': result['wall_seconds'],
                'peak_rss_bytes': result.get('peak_rss_bytes'), 'cache_hits': info.get('cache_hits', []),
                'candidate': reference(candidate)}
        seen = set(); known_ids = {record['id'] for record in read()}
        selected_cases = spec.get('cases', ['M1_P1', 'M2_D1'])
        if not selected_cases or not set(selected_cases) <= {'M1_P1', 'M2_D1'}:
            raise ValueError('搜索案例必须为 M1_P1/M2_D1')
        for config_key in accepted:
            candidate = output / 'builds' / config_key
            if not (candidate / 'build.json').exists():
                continue
            hashes = load(candidate / 'build.json')['sha256']
            for case in selected_cases:
                program = 'programs/' + case + '.asm'
                if config_key != anchor_key and hashes['hardware.json'] == anchor_hashes['hardware.json'] and hashes[program] == anchor_hashes[program]:
                    unchanged += 1
                    continue
                key = hashlib.sha256(canonical({'hardware': hashes['hardware.json'], 'program': hashes[program],
                                               'case': case, 'seeds': seeds, 'engine': identity['engine']})).hexdigest()
                if key in seen:
                    duplicates += 1
                    continue
                seen.add(key)
                directory = output / 'reports'; directory.mkdir(exist_ok=True)
                attempts = sorted((report for report in directory.glob(key + '.attempt*.json')
                    if re.fullmatch(re.escape(key) + r'\.attempt\d+\.json', report.name)),
                    key=lambda report: int(report.stem.split('attempt')[-1]))
                completed = [report for report in attempts if 'completed_without_error' in load(report)]
                if completed:
                    report = completed[-1]
                    attempt = report.stem.split('attempt')[-1]
                    identifier = output.name + '-' + key + '-' + attempt
                    result_path = output / 'eval-jobs' / (key + '-' + attempt + '.result.json')
                    if result_path.exists() and identifier not in known_ids:
                        record_result(identifier, output.name, candidate, case, report, load(result_path))
                    if result_path.exists():
                        observations.append(observation(identifier, candidate, case, report, load(result_path)))
                    reused += 1
                    continue
                numbers = [int(match[1]) for file in directory.glob(key + '.attempt*')
                           if (match := re.match(re.escape(key) + r'\.attempt(\d+)', file.name))]
                prefix = output.name + '-' + key + '-'
                numbers += [int(identifier[len(prefix):]) for identifier in known_ids
                            if identifier.startswith(prefix) and identifier[len(prefix):].isdigit()]
                attempt = str(max(numbers, default=0) + 1)
                report = directory / (key + '.attempt' + attempt + '.json')
                job_key = key + '-' + attempt
                details[job_key] = (candidate, case, report, output.name + '-' + key + '-' + attempt)
                jobs.append(task(python, candidate, case, seeds, report, job_key, memory, spec.get('cache', True)))
        remaining = budget - (time.monotonic() - started)
        def completed(result):
            candidate, case, report, identifier = details[result['key']]
            record_result(identifier, output.name, candidate, case, report, result)
            print(json.dumps({'event': 'task_finished', 'case': case, 'record': identifier,
                              'status': result['status'], 'wall_seconds': result['wall_seconds']},
                             ensure_ascii=False), flush=True)
        evaluated = run(jobs, output / 'eval-jobs', workers=workers, memory_fraction=fraction,
                        timeout=timeout, budget=max(.001, remaining), env=env, on_complete=completed)
        for result in evaluated['jobs']:
            candidate, case, report, identifier = details[result['key']]
            record_result(identifier, output.name, candidate, case, report, result)
            observations.append(observation(identifier, candidate, case, report, result))
        verify_official()
        current_source = {str(p.relative_to(ROOT)): digest(p) for p in sorted((ROOT / 'src').rglob('*.py'))}
        if source != current_source:
            raise ValueError('搜索期间源码发生变化，本批次不可晋升')
        summary = {'campaign': output.name, 'scope': 'parallel single-case exploration; not full grade',
            'wall_seconds': time.monotonic() - started, 'workers': evaluated['workers_limit'],
            'memory_budget_bytes': evaluated['memory_budget_bytes'], 'build': build_summary,
            'pruned': pruned, 'duplicate_tasks': duplicates, 'unchanged_cases': unchanged,
            'resumed_reports': reused, 'evaluation_calls': len(jobs), 'observations': observations}
        atomic_json(output / 'summary.json', summary)
        from ..report import generate
        generate()
        return summary
