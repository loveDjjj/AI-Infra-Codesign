import json,html
from .config import ROOT,load,digest
from .records import read,state
from .evaluation.profile import summarize,stage_pressure,stage_labels

def generate():
    (ROOT/'workspace/reports').mkdir(parents=True,exist_ok=True)
    records=read();profiles={}
    for record in records:
        profiles[record['id']]=record.get('profile',{})
        path=Path_report(record.get('report'))
        if path and path.exists() and (not record.get('profile') or record['id'] in [state().get('promoted_record'),state().get('archived_release')]):
            report=load(path)
            profiles[record['id']]={case:summarize(value['timing']) for case,value in report.get('cases',{}).items() if 'timing' in value and 'resource_stats' in value['timing']}
            if record['id'] not in [state().get('promoted_record'),state().get('archived_release')]:
                for info in profiles[record['id']].values():info['timeline']=None
    for case,prefix in [('M1_P1','p1'),('M2_D1','d1')]:
        file=ROOT/'data/releases/joint28'/f'{prefix}-profile.json'
        if file.exists():
            traced=load(file);target=next((key for key in profiles if key.startswith('joint28_v072')),None)
            if target:
                profiles[target][case]['operator_spans']=[stage|stage_labels(stage)|{'pressure_estimate':stage_pressure(stage,profiles[target][case].get('timeline'))} for stage in traced.get('operator_spans',[])]
                profiles[target][case]['sync_stages']=traced.get('stages',[])
                profiles[target][case]['stage_status']='Measured event trace, complete timing identical to original grade'
    for record in records:
        for case,evidence in record.get('profile_evidence',{}).items():
            file=Path_report(evidence.get('path'));original=Path_report(record.get('report'))
            if not file or not file.exists() or digest(file)!=evidence.get('sha256') or not original or not original.exists():continue
            traced=load(file)
            if traced.get('complete_timing_identical') is not True or traced.get('compare_sha256')!=digest(original):continue
            measured=summarize(traced['timing'])
            measured['operator_spans']=[stage|stage_labels(stage)|{'pressure_estimate':stage_pressure(stage,measured.get('timeline'))}
                for stage in traced.get('operator_spans',[])]
            measured['source_evidence']=evidence
            measured['stage_status']='实测事件轨迹，原报告时序一致，证据哈希已核对'
            if record['id'] not in [state().get('promoted_record'),state().get('archived_release')]:measured['timeline']=None
            profiles[record['id']][case]=measured
    host=[]
    for file in sorted((ROOT/'workspace').rglob('*.host.latest.json')):
        sample=load(file);log=file.with_name(file.name.replace('.latest.json','.jsonl'))
        last=load_tail(log) if log.exists() else {}
        sample['completed']=last.get('event')=='process_gone' or sample.get('state')=='Z'
        host.append({'experiment':file.stem,'sample':sample,'source':'host process tree, not simulated chip utilization'})
    campaigns=[]
    for metadata in sorted((ROOT/'workspace/search').glob('*/campaign.json')):
        directory=metadata.parent
        status_path=directory/'eval-jobs/status.json'
        if not status_path.exists():status_path=directory/'build-jobs/status.json'
        summary_path=directory/'summary.json'
        campaigns.append({'id':directory.name,'status':load(status_path) if status_path.exists() else {},
                          'summary':load(summary_path) if summary_path.exists() else None})
    pipelines=[]
    for runtime_file in sorted((ROOT/'workspace/pipeline').glob('*/state.json')):
        runtime=load(runtime_file);directory=runtime_file.parent
        status_file=directory/'status.json'
        status=load(status_file) if status_file.exists() else {}
        from .search.status import process_identity, attempt_costs, target_summary
        for process in status.get('active', []):
            process['identity_status']=process_identity(process['pid'],process.get('start_ticks'))
        targets=[target_summary(identifier,entry) for identifier,entry in runtime.get('targets',{}).items()]
        lanes=[{'lane':name,'pending':entry.get('pending'),
                'last_analysis_wall':entry.get('last_analysis_wall'),
                'running':bool(entry.get('running_job')),
                'recovery_blocked':entry.get('recovery_blocked',False),
                'last_error':entry.get('last_error'),
                'manual_requested':bool(entry.get('manual_request')),
                'independent_observations':len(entry.get('observations',[]))}
               for name,entry in runtime.get('analysis',{}).get('lanes',{}).items()]
        pipelines.append({'id':directory.name,'status':status,'targets':targets,'lanes':lanes,
            'attempt_costs':attempt_costs(runtime),
            'decisions':len(runtime.get('applied_decisions',{})),
            'updated_wall':status_file.stat().st_mtime if status_file.exists() else runtime_file.stat().st_mtime})
    implementations=[]
    for path in sorted((ROOT/'workspace/implementation-loop').glob('*/*/state.json')):
        item=load(path)
        implementations.append({'campaign':path.parents[1].name,'id':item.get('proposal',{}).get('id'),
            'case':item.get('proposal',{}).get('case'),'status':item.get('status'),
            'case_gain':item.get('case_gain'),'official_score':item.get('official_score'),
            'target_id':item.get('target_id'), 'recovery_attempts':item.get('recovery_attempts',0),
            'error':item.get('error') or item.get('last_error') or item.get('reason'),
            'updated_wall':item.get('updated_wall')})
    data={'pipelines':pipelines,'implementations':implementations,'host':host,'campaigns':campaigns,'records':[{key:value for key,value in record.items() if key!='profile'} for record in records],'state':state(),'profiles':profiles}
    payload=json.dumps(data,ensure_ascii=False).replace('<',r'\u003c')
    page=TEMPLATE.replace('__DATA__',payload)
    target=ROOT/'docs/dashboard.html';target.write_text(page)
    (ROOT/'workspace/reports/profile-summary.json').write_text(json.dumps({key:{case:{k:v for k,v in info.items() if k!='timeline'} for case,info in cases.items()} for key,cases in profiles.items()},ensure_ascii=False,indent=2)+'\n')
    return {'dashboard':str(target),'records':len(records),'profiles':len(profiles)}
def load_tail(path):
    lines=path.read_text().splitlines()
    return json.loads(lines[-1]) if lines else {}
def Path_report(value):
    from pathlib import Path
    if not value:return None
    path=Path(value);return path if path.is_absolute() else ROOT/path

TEMPLATE = r'''<!doctype html><html lang="zh"><meta charset="utf-8"><title>Codesign Lab</title>
<style>body{font:15px system-ui;background:#0b1424;color:#dce6f5;margin:24px;max-width:1400px}h1,h2{color:#90c7ff}section{background:#162239;padding:20px;margin:16px 0;border-radius:12px}select,input{padding:8px;background:#243854;color:white;border:1px solid #668}table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:8px;border-bottom:1px solid #354561}.cards{display:flex;gap:20px;flex-wrap:wrap}.card{padding:12px;background:#243854;border-radius:8px}button{cursor:pointer;padding:6px}canvas{width:100%;height:270px}.note{color:#ffc47c}pre{white-space:pre-wrap;max-height:450px;overflow:auto}tr:hover{background:#243854}</style>
<h1>AI Infra · 研究工作台</h1><p>配置定义设计，账本保存事实；测量、估计与假设分开显示。页面由 ./lab report 生成。</p>
<section><h2>当前状态</h2><div class="cards" id="status"></div></section>
<section><h2>动态流水线与验收</h2><p>生成时快照；更新时刻是状态文件时间，旧状态不能证明进程仍然存活。预测与正式评分分别记录。</p><table><thead><tr><th>流水线</th><th>总槽位 / 验收预留</th><th>活跃 / 排队</th><th>冷 case / full 已使用</th><th>目标 / 决策</th><th>状态更新时间</th></tr></thead><tbody id="pipelines"></tbody></table></section>
<section><h2>隔离结构实验</h2><p>AI 提案进入统一调度；正确且功耗合格的新结构可在原批次继续有限搜索。正式成绩仍由冻结官方整案和审计决定。失败记录不等于性能观测。</p><table><thead><tr><th>批次 / 提案</th><th>案例</th><th>关卡 / 续搜目标</th><th>单案收益</th><th>官方分数</th><th>恢复次数 / 失败原因</th></tr></thead><tbody id="implementations"></tbody></table></section>
<section><h2>主控资源、等待与重试</h2><p>主机调度实测；等待原因是准入条件，不是模拟芯片 stall。累计任务秒可并行重叠，不等于墙钟时间。</p><table><thead><tr><th>流水线</th><th>内存预算 / 预留 / 可准入 MiB</th><th>已记录尝试 / 累计秒 / 重试秒</th><th>恢复估计次数</th></tr></thead><tbody id="pipelineCosts"></tbody></table><h3>排队任务</h3><table><thead><tr><th>流水线 / 任务</th><th>阶段</th><th>等待秒</th><th>准入等待原因</th></tr></thead><tbody id="pipelineQueue"></tbody></table><h3>评估监督进程</h3><table><thead><tr><th>流水线 / 任务</th><th>阶段</th><th>PID / 身份</th><th>准入等待秒</th><th>运行秒 / RSS MiB</th></tr></thead><tbody id="pipelineProcesses"></tbody></table></section>
<section><h2>目标采样与反馈</h2><p>提案来源是后端接口记录；TPE 启动期仍可能是随机提案。缓存反馈不计独立性能观测，终态恢复检查不代表采样收益。</p><table><thead><tr><th>流水线 / 目标</th><th>采样 / 状态</th><th>完成 / 提出 / 预算</th><th>提案来源</th><th>待反馈 / 后端任务</th><th>本轮独立观测 / 历史先验 / 拒绝 / 待处理</th></tr></thead><tbody id="samplingTargets"></tbody></table></section><section><h2>分析请求与研究方向</h2><p>待分析请求不等于真实 AI 已执行；离线决策用于链路验证。</p><table><thead><tr><th>流水线 / lane</th><th>独立观测</th><th>待分析原因</th><th>执行 / 恢复状态</th><th>当前假设</th></tr></thead><tbody id="analysisLanes"></tbody></table></section>
<section><h2>并行搜索批次</h2><p>生成时的快照；运行状态实时文件位于各批次 eval-jobs/status.json。候选单案结果不是整包得分。</p><table><thead><tr><th>批次</th><th>进程上限</th><th>运行 / 排队 / 完成</th><th>内存预算 MiB</th><th>批次耗时 s</th></tr></thead><tbody id="campaigns"></tbody></table></section>
<section><h2>实验账本</h2><input id="query" placeholder="搜索实验名 / 范围"><table><thead><tr><th>实验</th><th>范围</th><th>得分</th><th>合格</th><th>主机秒</th></tr></thead><tbody id="rows"></tbody></table></section>
<section><h2>芯片资源与瓶颈</h2><select id="run"></select> <select id="case"><option>M1_P1</option><option>M2_D1</option></select> <select id="sm"><option value="shared">共享资源</option></select><div id="metrics" class="cards"></div><p class="note" id="wait"></p><canvas id="heat" width="1280" height="270"></canvas><p>热图为资源服务时间占比，可并行重叠。分箱热图不能替代精确功耗门槛。</p><table><thead><tr><th>实体</th><th>资源</th><th>忙碌比例</th><th>服务周期</th></tr></thead><tbody id="resources"></tbody></table><h3>算子阶段</h3><p id="stageNote"></p><table><thead><tr><th>阶段 / 层 / 步</th><th>算子 / 名称</th><th>开始</th><th>结束</th><th>跨度</th><th>资源压力推算</th></tr></thead><tbody id="stages"></tbody></table><details><summary>完整配置与证据 / AI 可读分析</summary><pre id="detail"></pre></details></section>
<section><h2>评估主机监控</h2><p>CPU、RSS、进程状态、等待通道和 I/O 来自评测进程树；仅是生成时快照，重新执行 ./lab report 刷新。</p><table><thead><tr><th>任务</th><th>采样时间</th><th>CPU 单核 %</th><th>RSS MiB</th><th>状态 / 等待通道</th><th>I/O 读写增量字节</th></tr></thead><tbody id="hosts"></tbody></table></section><section><h2>搜索与下一步</h2><p>搜索空间生成、去重、合法性剪枝与评测分别实现。面积是硬剪枝；历史功耗或低利用率只影响探索方向。搜索支持显式执行；动态流水线按预算组合入围候选，并通过冻结官方通道验收。网站上传保持关闭。</p><p>MILP 尚未接入：应先明确整数决策变量、可证明的约束和局部目标，再用真实子问题对比求解速度与最优间隙。不能用资源忙碌比例伪造完整性能目标。</p></section>
<script type="application/json" id="data">__DATA__</script><script>
const D=JSON.parse(document.getElementById('data').textContent),$=id=>document.getElementById(id),fmt=x=>x==null||(typeof x==='number'&&!Number.isFinite(x))?'未提供':typeof x==='number'?x.toLocaleString('en-US',{maximumFractionDigits:2}):String(x),esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const highest=D.records.filter(r=>r.audited&&r.eligible&&r.score!=null).sort((a,b)=>b.score-a.score)[0];
$('status').innerHTML=[['最高已审计',highest?.id],['已晋升',D.state.promoted_record],['已归档版本',D.state.archived_release],['已提交（有收据）',D.state.submitted_release]].map(([label,value])=>`<div class="card">${label}<br><b>${esc(value||'未设置')}</b></div>`).join('');
$('campaigns').innerHTML=(D.campaigns||[]).map(c=>`<tr><td>${esc(c.id)}</td><td>${fmt(c.status.workers_limit)}</td><td>${fmt(c.status.active?.length)} / ${fmt(c.status.pending)} / ${fmt(c.status.completed)}</td><td>${fmt(c.status.memory_budget_bytes/1024/1024)}</td><td>${fmt(c.summary?.wall_seconds??c.status.elapsed_seconds)}</td></tr>`).join('');
$('pipelines').innerHTML=(D.pipelines||[]).map(p=>`<tr><td>${esc(p.id)}</td><td>${fmt(p.status.workers_limit)} / ${fmt(p.status.full_slots)}</td><td>${fmt(p.status.active?.length)} / ${fmt(Object.values(p.status.pending||{}).reduce((a,b)=>a+b,0))}</td><td>${fmt(p.status.budget?.used?.case)} / ${fmt(p.status.budget?.used?.full)}</td><td>${fmt(p.targets.length)} / ${fmt(p.decisions)}</td><td>${esc(new Date(p.updated_wall*1000).toISOString())}</td></tr>`).join('');
$('implementations').innerHTML=(D.implementations||[]).map(x=>`<tr><td>${esc(x.campaign+' / '+x.id)}</td><td>${esc(x.case)}</td><td>${esc(x.status)}${x.target_id?' / '+esc(x.target_id):''}</td><td>${x.case_gain==null?'—':fmt(x.case_gain*100)+'%'}</td><td>${fmt(x.official_score)}</td><td>${fmt(x.recovery_attempts)} / ${esc(x.error||'')}</td></tr>`).join('');
$('samplingTargets').innerHTML=(D.pipelines||[]).flatMap(p=>p.targets.map(t=>`<tr><td>${esc(p.id+' / '+t.id)}</td><td>${esc(t.sampler+' / '+t.status)}${t.error?'<br>'+esc(t.error):''}</td><td>${fmt(t.completed)} / ${fmt(t.launched)} / ${fmt(t.budget)}</td><td>${esc(Object.entries(t.origins||{}).map(([k,v])=>k+': '+v).join(', ')||'固定序列')}</td><td>${fmt(t.feedback_pending)} / ${fmt(t.sampling_jobs_pending)}</td><td>${fmt(t.independent_observations)} / ${fmt(t.historical_priors)} / ${fmt(t.priors_rejected)} / ${fmt(t.priors_pending)}</td></tr>`)).join('');
$('analysisLanes').innerHTML=(D.pipelines||[]).flatMap(p=>p.lanes.map(l=>`<tr><td>${esc(p.id+' / '+l.lane)}</td><td>${fmt(l.independent_observations)}</td><td>${esc(l.pending?.reasons?.join(', ')||'无待处理请求')}</td><td>${esc(l.recovery_blocked?'恢复待核对':l.running?'监督任务在途':l.manual_requested?'手动请求等待准入':'未执行')}<br>${esc(l.last_error||'')}</td><td>${esc(p.targets.filter(t=>t.lane===l.lane).map(t=>t.id+' ['+t.status+'] '+t.hypothesis).join('；'))}</td></tr>`)).join('');
$('pipelineCosts').innerHTML=(D.pipelines||[]).map(p=>`<tr><td>${esc(p.id)}</td><td>${fmt(p.status.memory_budget_bytes/1048576)} / ${fmt(p.status.reserved_memory_bytes/1048576)} / ${fmt(p.status.available_memory_admission_bytes/1048576)}</td><td>${fmt(p.attempt_costs?.attempts)} / ${fmt(p.attempt_costs?.attempt_host_seconds)} / ${fmt(p.attempt_costs?.retry_host_seconds)}</td><td>${fmt(p.attempt_costs?.estimated_attempts)}</td></tr>`).join('');
$('pipelineQueue').innerHTML=(D.pipelines||[]).flatMap(p=>(p.status.queue||[]).map(j=>`<tr><td>${esc(p.id+' / '+j.key)}</td><td>${esc(j.stage)}</td><td>${fmt(j.queue_seconds)}</td><td>${esc(j.reason)}</td></tr>`)).join('');
$('pipelineProcesses').innerHTML=(D.pipelines||[]).flatMap(p=>(p.status.active||[]).map(j=>`<tr><td>${esc(p.id+' / '+j.key)}</td><td>${esc(j.stage)}</td><td>${fmt(j.pid)} / ${esc(j.identity_status)}</td><td>${fmt(j.queue_wait_seconds)}</td><td>${fmt(j.wall_seconds)} / ${fmt(j.peak_rss_bytes/1048576)}</td></tr>`)).join('');
function rows(){let q=$('query').value.toLowerCase();$('rows').innerHTML=D.records.filter(r=>(r.id+' '+r.scope).toLowerCase().includes(q)).map(r=>`<tr><td>${esc(r.id)}</td><td>${esc(r.scope)}</td><td>${fmt(r.score)}</td><td>${r.eligible==null?'未评定':r.eligible?'通过':'未通过'}</td><td>${fmt(r.host_seconds)}</td></tr>`).join('')}
$('hosts').innerHTML=D.host.map(h=>`<tr><td>${esc(h.experiment)}</td><td>${new Date(h.sample.timestamp*1000).toISOString()}</td><td>${fmt(h.sample.cpu_percent_one_core)}</td><td>${fmt((h.sample.tree_rss_bytes||h.sample.rss_bytes)/1024/1024)}</td><td>${esc((h.sample.completed?'已结束，最后采样 ':'')+h.sample.state+' / '+h.sample.wchan)}</td><td>${fmt(h.sample.io_delta?.read_bytes)} / ${fmt(h.sample.io_delta?.write_bytes)}</td></tr>`).join('');$('query').oninput=rows;rows();$('run').innerHTML=Object.keys(D.profiles).filter(id=>Object.keys(D.profiles[id]).length).map(id=>`<option>${esc(id)}</option>`).join('');if(highest&&D.profiles[highest.id])$('run').value=highest.id;for(let n=0;n<16;n++)$('sm').innerHTML+=`<option value="${n}">SM ${n}</option>`;
function draw(){const id=$('run').value,caseName=$('case').value,p=D.profiles[id]?.[caseName];if(!p){$('metrics').textContent='未提供该案资源报告';$('resources').innerHTML='';$('stages').innerHTML='';$('wait').textContent='等待原因未提供';$('stageNote').textContent='算子阶段未提供';$('detail').textContent='该评估范围没有资源报告';$('heat').getContext('2d').clearRect(0,0,1280,270);return}const r=D.records.find(r=>r.id===id),t=r.cases[caseName]?.timing||{};
$('metrics').innerHTML=[['周期',p.cycles],['精确峰值 W',t.peak_window_power_w],['HBM 读字节',t.hbm_read_bytes],['HBM 写字节',t.hbm_write_bytes],['资源压力最高',p.pressure_leader?.resource]].map(([k,v])=>`<div class="card">${k}<br><b>${esc(fmt(v))}</b></div>`).join('');
$('wait').textContent=`等待代理估计：${fmt((p.wait_estimate.fraction??0)*100)}%（低置信度）。这是未被最高资源占用率覆盖的容量，不是实测等待；可能来自依赖、负载不均、同步或调度，无法区分原因。`;
const resources=p.resources.filter(r=>$('sm').value==='shared'?r.scope==='shared':r.scope==='sm'&&r.entity===$('sm').value);$('resources').innerHTML=resources.map(r=>`<tr><td>${esc(r.entity)}</td><td>${esc(r.resource)}</td><td>${fmt(r.utilization*100)}%</td><td>${fmt(r.busy_cycles)}</td></tr>`).join('');
$('stageNote').textContent=p.stage_status+'；算子跨度可能重叠或嵌套，不能相加当作总周期。';$('stages').innerHTML=(p.operator_spans||[]).map(s=>`<tr><td>${esc(s.phase)} / ${fmt(s.layer)} / ${fmt(s.decode_step)}</td><td>${esc(s.operator+' '+s.name)}</td><td>${fmt(s.start)}</td><td>${fmt(s.finish)}</td><td>${fmt(s.span_cycles)}</td><td>${esc(s.pressure_estimate?.leader?.resource||'未提供')}（低置信度）</td></tr>`).join('');$('detail').textContent=JSON.stringify({record:r,analysis:{...p,timeline:undefined}},null,2);
const ctx=$('heat').getContext('2d');ctx.clearRect(0,0,1280,270);ctx.font='12px system-ui';const timeline=p.timeline,selection=$('sm').value;let group=selection==='shared'?timeline?.shared:timeline?.sms?.[selection];
if(!group){ctx.fillStyle='#fff';ctx.fillText('无时间分箱数据',10,30);return}const entries=Object.entries(group.utilization||group.bandwidth_utilization||group);let row=0;for(const [name,values] of entries){if(!Array.isArray(values))continue;const y=row++*24;ctx.fillStyle='#fff';ctx.fillText(name,0,y+15);values.forEach((v,i)=>{const number=typeof v==='number'?v:0;ctx.fillStyle=`rgba(64,180,255,${Math.min(1,Math.max(.08,number))})`;ctx.fillRect(130+i*1100/values.length,y,1100/values.length,19)})}}
$('run').onchange=draw;$('case').onchange=draw;$('sm').onchange=draw;draw();
</script></html>'''
