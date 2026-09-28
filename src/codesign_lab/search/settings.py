"""统一流水线配置，只接受已实现的字段，不静默忽略未知设置。"""
from pathlib import Path
from ..config import ROOT, load

FIELDS = {
    'scheduler': {'workers': 'workers', 'full_slots': 'full-slots',
                  'memory_fraction': 'memory-fraction', 'timeout_seconds': 'timeout',
                  'max_retries': 'max-retries', 'report_interval_seconds': 'report-interval'},
    'budget': {'wall_seconds': 'budget', 'max_proposals': 'max-proposals',
               'case_calls': 'max-case-calls', 'full_calls': 'max-full-calls',
               'ai_calls': 'max-ai-calls', 'profile_calls': 'max-profile-calls'},
    'full_grade': {'min_predicted_gain': 'min-predicted-gain', 'max_pending': 'max-pending-full'},
    'ai': {'timeout_seconds': 'ai-timeout', 'mode': 'analysis-mode'},
    'triggers': {'batch_size': 'analysis-batch-size', 'cooldown_seconds': 'analysis-cooldown',
                 'low_watermark': 'analysis-low-watermark', 'stagnation_trials': 'stagnation-trials',
                 'improvement_threshold': 'improvement-threshold',
                 'failure_window': 'failure-window', 'failure_threshold': 'failure-threshold'},
}


def arguments(path, *, execute=False, resume=False):
    settings = load(path)
    allowed = {'schema_version', 'out', 'search_configs', 'watch', 'stay_open', 'implementation',
               'stop_on_exhaustion', 'auto_audit', 'cache_dir', *FIELDS}
    if set(settings) - allowed or settings.get('schema_version') != 1:
        raise ValueError('未知流水线字段或 schema_version')
    if not isinstance(settings.get('out'), str):
        raise ValueError('配置 out 必须是路径字符串')
    out = ROOT / settings['out']
    if not out.resolve().is_relative_to(ROOT/'workspace/pipeline'):
        raise ValueError('配置 out 必须位于 workspace/pipeline')
    argv = ['--out', str(out)]
    if 'cache_dir' in settings:
        if not isinstance(settings['cache_dir'],str):raise ValueError('cache_dir 必须是路径字符串')
        cache=(ROOT/settings['cache_dir']).resolve()
        if not cache.is_relative_to(ROOT/'workspace'):raise ValueError('探索缓存必须位于 workspace')
        argv.extend(['--cache-dir',str(cache)])
    for group, mapping in FIELDS.items():
        values = settings.get(group, {})
        if not isinstance(values, dict) or set(values) - (set(mapping) | ({'enabled'} if group == 'ai' else set())):
            raise ValueError('未知流水线配置组字段：' + group)
        for name, value in values.items():
            if name == 'enabled':
                if type(value) is not bool: raise ValueError('ai.enabled 必须是布尔值')
                if value: argv.append('--ai-enabled')
            elif name == 'mode':
                if value not in {'per_lane','global'}:raise ValueError('ai.mode 必须为 per_lane 或 global')
                argv.extend(['--analysis-mode',value])
            else:
                if type(value) not in (int, float): raise ValueError('数值字段无效：' + name)
                argv.extend(['--' + mapping[name], str(value)])
    for name, flag in [('stay_open', '--stay-open'), ('auto_audit', '--no-auto-audit')]:
        if name in settings and type(settings[name]) is not bool:
            raise ValueError(name + ' 必须是布尔值')
        if settings.get(name, name == 'auto_audit') == (name == 'stay_open'):
            argv.append(flag)
    if 'stop_on_exhaustion' in settings:
        if type(settings['stop_on_exhaustion']) is not bool:
            raise ValueError('stop_on_exhaustion 必须是布尔值')
        if settings['stop_on_exhaustion']:
            argv.append('--stop-on-exhaustion')
    implementation = settings.get('implementation', {})
    if not isinstance(implementation, dict) or set(implementation)-{
            'enabled','model','reasoning_effort','max_proposals','min_case_gain','min_score_gain'}:
        raise ValueError('未知结构实验配置字段')
    if 'enabled' in implementation and type(implementation['enabled']) is not bool:
        raise ValueError('implementation.enabled 必须为布尔值')
    if implementation.get('enabled', False):
        if 'model' in implementation and (not isinstance(implementation['model'], str) or not implementation['model']):
            raise ValueError('implementation.model 无效')
        if 'reasoning_effort' in implementation and implementation['reasoning_effort'] not in {'low','medium','high','xhigh','max'}:
            raise ValueError('implementation.reasoning_effort 无效')
        for name in ('max_proposals','min_case_gain','min_score_gain'):
            if name in implementation and type(implementation[name]) not in (int,float):
                raise ValueError('implementation.'+name+' 必须为数值')
        argv.append('--implementation-enabled')
        for name, flag in [('model','implementation-model'),
                           ('reasoning_effort','implementation-effort'),
                           ('max_proposals','implementation-max-proposals'),
                           ('min_case_gain','implementation-min-case-gain'),
                           ('min_score_gain','implementation-min-score-gain')]:
            if name in implementation:
                argv.extend(['--'+flag, str(implementation[name])])
    configs = settings.get('search_configs', [])
    watches = settings.get('watch', [])
    if not isinstance(configs, list) or not isinstance(watches, list):
        raise ValueError('search_configs/watch 必须是列表')
    for config in configs:
        if not isinstance(config, str): raise ValueError('搜索配置路径无效')
        source = (ROOT / config).resolve()
        if not source.is_relative_to(ROOT/'configs') or not source.is_file():
            raise ValueError('搜索配置必须是 configs 下的现有文件')
        argv.extend(['--config', str(source)])
    for watch in watches:
        if not isinstance(watch, str) or Path(watch).name != watch: raise ValueError('watch 名称无效')
        argv.extend(['--watch', watch])
    if execute: argv.append('--execute')
    if resume: argv.append('--resume')
    return argv
