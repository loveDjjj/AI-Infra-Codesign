"""生成轻量只读看板：最佳指标、优化趋势和真实在途任务。"""
import html
import json
import time
from datetime import datetime, timezone

from .config import ROOT, load
from .records import read, state
from .search.status import process_identity

_ledger_cache=(None,None)


def _records_cached():
    """账本未变化时不在每次浏览器轮询中重读大型历史记录。"""
    global _ledger_cache
    path=ROOT/'data/experiments.jsonl'
    identity=(path.stat().st_mtime_ns,path.stat().st_size) if path.exists() else None
    if _ledger_cache[1] is None or _ledger_cache[0]!=identity:
        _ledger_cache=(identity,read())
    return _ledger_cache[1]


def _text(value):
    return html.escape(str(value if value is not None else '—'), quote=True)


def _number(value, digits=2):
    return f'{value:,.{digits}f}' if isinstance(value, (int, float)) else '—'


def _series(rows, field, *, lower=False):
    """只展示已审计合格整案的累计最佳值，不把局部结果画成总分。"""
    values=[]
    best=None
    for row in rows:
        value=field(row)
        if not isinstance(value, (int, float)):
            continue
        best=min(best,value) if lower and best is not None else max(best,value) if best is not None else value
        values.append(float(best))
    return values


def _chart(title, values, unit='', color='#58c7ff'):
    if not values:
        return f'<section class="chart"><h2>{_text(title)}</h2><p>尚无已审计合格整案数据</p></section>'
    low=min(values);high=max(values);span=max(high-low,1)
    points=' '.join(f'{30+i*620/max(len(values)-1,1):.1f},{155-(value-low)*125/span:.1f}'
                    for i,value in enumerate(values))
    circles=''.join(f'<circle cx="{30+i*620/max(len(values)-1,1):.1f}" '
                    f'cy="{155-(value-low)*125/span:.1f}" r="3" fill="{color}"/>'
                    for i,value in enumerate(values))
    return (f'<section class="chart"><h2>{_text(title)}</h2>'
            f'<svg viewBox="0 0 680 190" role="img" aria-label="{_text(title)}趋势图">'
            '<path d="M30 160H650" stroke="#49607a"/>'
            f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="3"/>{circles}'
            f'<text x="30" y="181">起点 {_text(_number(values[0],0))}{_text(unit)}</text>'
            f'<text x="650" y="181" text-anchor="end">当前 {_text(_number(values[-1],0))}{_text(unit)}</text>'
            '</svg></section>')


def _active_tasks():
    """旧状态文件不能证明进程仍在运行，逐个核对 PID 启动身份。"""
    tasks=[]
    for path in (ROOT/'workspace/pipeline').glob('*/status.json'):
        try:
            status=load(path)
            for job in status.get('active',[]):
                if process_identity(job['pid'],job.get('start_ticks'))!='身份匹配，生成时存活':
                    continue
                tasks.append({'campaign':path.parent.name,'stage':job.get('stage'),
                    'key':job.get('key'),'seconds':job.get('wall_seconds'),
                    'rss_mib':(job.get('peak_rss_bytes') or 0)/1048576})
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return tasks


def _latest_ai():
    path=ROOT/'data/decisions.jsonl'
    if not path.is_file():
        return None
    for line in reversed(path.read_text().splitlines()):
        if line.strip():
            try:
                row=json.loads(line)
                return {'summary':row.get('decision',{}).get('summary'),
                        'lane':row.get('lane'),'id':row.get('id')}
            except ValueError:
                continue
    return None


def _supply_status():
    """只读最新未完批次的供给解释，避免把空队列误报为进程故障。"""
    candidates = sorted((ROOT/'workspace/pipeline').glob('*/state.json'),
                        key=lambda path: path.stat().st_mtime_ns, reverse=True)
    for path in candidates:
        if (path.parent/'summary.json').exists():
            continue
        status_path=path.parent/'status.json'
        if not status_path.is_file() or time.time()-status_path.stat().st_mtime > 120:
            continue
        try:
            value=load(path)
            supply=value.get('supply',{})
            budget=value.get('budget',{})
            return {'campaign':path.parent.name,
                'coverage_seconds':supply.get('coverage_seconds'),
                'ready_tasks':supply.get('ready_tasks'),
                'active_family_tasks':supply.get('active_family_tasks'),
                'blocked_reason':supply.get('blocked_reason'),
                'ai_attempts':supply.get('attempts',0),
                'remaining_wall_seconds':max(0,budget.get('deadline',0)-datetime.now(timezone.utc).timestamp())}
        except (OSError, ValueError, TypeError):
            continue
    return None


def live_snapshot():
    """HTTP 看板只返回当前指标、短趋势和实时任务，不传历史原件。"""
    rows=_records_cached()
    audited=sorted((row for row in rows if row.get('scope')=='full' and
        row.get('audited') is True and row.get('eligible') is True and
        isinstance(row.get('score'),(int,float))),key=lambda row:row.get('timestamp',''))
    best=max(audited,key=lambda row:row['score']) if audited else None
    points=[];best_score=None;best_p1=None;best_d1=None
    for row in audited:
        score=row['score'];best_score=max(best_score,score) if best_score is not None else score
        cases=row.get('cases',{})
        p1=cases.get('M1_P1',{}).get('timing',{}).get('cycles')
        d1=cases.get('M2_D1',{}).get('timing',{}).get('cycles')
        if isinstance(p1,(int,float)):best_p1=min(best_p1,p1) if best_p1 is not None else p1
        if isinstance(d1,(int,float)):best_d1=min(best_d1,d1) if best_d1 is not None else d1
        points.append({'id':row['id'],'score':best_score,'p1_cycles':best_p1,
                       'd1_cycles':best_d1,'timestamp':row.get('timestamp')})
    cases=best.get('cases',{}) if best else {}
    from .release import champion_state
    champion=champion_state()
    def metric(case,name):
        return cases.get(case,{}).get('timing',{}).get(name)
    return {'updated_at':datetime.now(timezone.utc).isoformat(),
        'best':{'id':best['id'],'score':best['score'],
                'p1_cycles':metric('M1_P1','cycles'),'d1_cycles':metric('M2_D1','cycles'),
                'p1_power_w':metric('M1_P1','peak_window_power_w'),
                'd1_power_w':metric('M2_D1','peak_window_power_w')} if best else None,
        'promoted_record':state().get('promoted_record'),
        'best_submittable':champion,
        'trend':points,'tasks':_active_tasks(),'ai':_latest_ai(),
        'supply':_supply_status()}


def generate():
    records=read()
    audited=sorted((row for row in records if row.get('scope')=='full' and
        row.get('audited') is True and row.get('eligible') is True and
        isinstance(row.get('score'),(int,float))),key=lambda row:row.get('timestamp',''))
    best=max(audited,key=lambda row:row['score']) if audited else None
    cases=best.get('cases',{}) if best else {}
    tasks=_active_tasks()
    pointer=state()
    from .release import champion_state
    champion=champion_state()
    now=datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    cards=[('最高已审计分数',_number(best['score']) if best else '—'),
           ('P1 周期',_number(cases.get('M1_P1',{}).get('timing',{}).get('cycles'),0)),
           ('D1 周期',_number(cases.get('M2_D1',{}).get('timing',{}).get('cycles'),0)),
           ('当前运行任务',str(len(tasks))),
           ('可提交冠军',_number(champion['score']) if champion else '—')]
    card_html=''.join(f'<div class="card"><span>{_text(label)}</span><strong>{_text(value)}</strong></div>'
                      for label,value in cards)
    graphs=''.join([
        _chart('最高分走势',_series(audited,lambda row:row['score']),' 分','#67e4b4'),
        _chart('P1 最少周期',_series(audited,lambda row:row.get('cases',{}).get('M1_P1',{}).get('timing',{}).get('cycles'),lower=True),' 周期'),
        _chart('D1 最少周期',_series(audited,lambda row:row.get('cases',{}).get('M2_D1',{}).get('timing',{}).get('cycles'),lower=True),' 周期','#ffc178')])
    task_rows=''.join('<tr>'+''.join(f'<td>{_text(value)}</td>' for value in
        (task['campaign'],task['stage'],str(task['key'])[:28],_number(task['seconds'],0)+' s',
         _number(task['rss_mib'],0)+' MiB'))+'</tr>' for task in tasks)
    if not task_rows:task_rows='<tr><td colspan="5">当前没有核实仍在运行的任务</td></tr>'
    latest=_latest_ai()
    ai_html=(f'<p>{_text(latest["summary"] or "暂无摘要")}</p><small>决策 ID：{_text(latest["id"])}</small>'
             if latest else '<p>暂无 AI 归档摘要</p>')
    best_name=_text(best['id']) if best else '尚无已审计成绩'
    body=f'''<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="30"><title>AI Infra 看板</title>
<style>body{{font:16px system-ui,sans-serif;background:#0b1424;color:#e6eef7;margin:0 auto;padding:28px;max-width:1200px}}
h1{{margin:0 0 4px}}h2{{font-size:18px;color:#9bd4ff}}small,.muted{{color:#9aacc3}}
.cards,.graphs{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px}}
.card,section{{background:#17263b;border:1px solid #29415d;border-radius:12px;padding:18px;margin:12px 0}}
.card span{{display:block;color:#a9bfd6}}.card strong{{display:block;font-size:28px;margin-top:8px;color:#fff}}
svg{{width:100%;height:auto}}svg text{{fill:#b8cadc;font:12px system-ui}}table{{width:100%;border-collapse:collapse}}
th,td{{padding:10px;text-align:left;border-bottom:1px solid #344961;word-break:break-word}}
.scroll{{overflow-x:auto}}</style></head><body>
<h1>AI Infra · 精简看板</h1><p class="muted">生成于 {now}；页面每 30 秒重新读取。完整事实仍以实验账本和官方报告为准。</p>
<div class="cards">{card_html}</div>
<section><h2>当前最佳</h2><p>{best_name}</p><p>已晋升版本：{_text(pointer.get('promoted_record'))}。可提交 ZIP：{_text(champion.get('archive') if champion else None)}。趋势仅使用已审计合格整案，单案最快值不代表可直接组合的整案成绩。</p></section>
<div class="graphs">{graphs}</div>
<section><h2>正在运行的任务</h2><div class="scroll"><table><thead><tr><th>批次</th><th>阶段</th><th>任务</th><th>运行时间</th><th>峰值 RSS</th></tr></thead><tbody>{task_rows}</tbody></table></div></section>
<section><h2>最新 AI 整理</h2>{ai_html}</section>
</body></html>'''
    target=ROOT/'docs/dashboard.html'
    target.write_text(body)
    return {'dashboard':str(target),'bytes':target.stat().st_size,
            'audited_scores':len(audited),'active_tasks':len(tasks)}
