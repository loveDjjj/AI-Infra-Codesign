import json,html
from .config import ROOT,load
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
    host=[]
    for file in sorted((ROOT/'workspace').rglob('*.host.latest.json')):
        sample=load(file);log=file.with_name(file.name.replace('.latest.json','.jsonl'))
        last=load_tail(log) if log.exists() else {}
        sample['completed']=last.get('event')=='process_gone' or sample.get('state')=='Z'
        host.append({'experiment':file.stem,'sample':sample,'source':'host process tree, not simulated chip utilization'})
    data={'host':host,'records':[{key:value for key,value in record.items() if key!='profile'} for record in records],'state':state(),'profiles':profiles}
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
<section><h2>实验账本</h2><input id="query" placeholder="搜索实验名 / 范围"><table><thead><tr><th>实验</th><th>范围</th><th>得分</th><th>合格</th><th>主机秒</th></tr></thead><tbody id="rows"></tbody></table></section>
<section><h2>芯片资源与瓶颈</h2><select id="run"></select> <select id="case"><option>M1_P1</option><option>M2_D1</option></select> <select id="sm"><option value="shared">共享资源</option></select><div id="metrics" class="cards"></div><p class="note" id="wait"></p><canvas id="heat" width="1280" height="270"></canvas><p>热图为资源服务时间占比，可并行重叠。分箱热图不能替代精确功耗门槛。</p><table><thead><tr><th>实体</th><th>资源</th><th>忙碌比例</th><th>服务周期</th></tr></thead><tbody id="resources"></tbody></table><h3>算子阶段</h3><p id="stageNote"></p><table><thead><tr><th>阶段 / 层 / 步</th><th>算子 / 名称</th><th>开始</th><th>结束</th><th>跨度</th><th>资源压力推算</th></tr></thead><tbody id="stages"></tbody></table><details><summary>完整配置与证据 / AI 可读分析</summary><pre id="detail"></pre></details></section>
<section><h2>评估主机监控</h2><p>CPU、RSS、进程状态、等待通道和 I/O 来自评测进程树；仅是生成时快照，重新执行 ./lab report 刷新。</p><table><thead><tr><th>任务</th><th>采样时间</th><th>CPU 单核 %</th><th>RSS MiB</th><th>状态 / 等待通道</th><th>I/O 读写增量字节</th></tr></thead><tbody id="hosts"></tbody></table></section><section><h2>搜索与下一步</h2><p>搜索空间生成、去重、合法性剪枝与评测分别实现。面积是硬剪枝；历史功耗或低利用率只影响探索方向。当前搜索命令生成候选，不自动发起昂贵整包评测。</p><p>MILP 尚未接入：应先明确整数决策变量、可证明的约束和局部目标，再用真实子问题对比求解速度与最优间隙。不能用资源忙碌比例伪造完整性能目标。</p></section>
<script type="application/json" id="data">__DATA__</script><script>
const D=JSON.parse(document.getElementById('data').textContent),$=id=>document.getElementById(id),fmt=x=>x==null?'未提供':typeof x==='number'?x.toLocaleString('en-US',{maximumFractionDigits:2}):String(x),esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const highest=D.records.filter(r=>r.audited&&r.eligible&&r.score!=null).sort((a,b)=>b.score-a.score)[0];
$('status').innerHTML=[['最高已审计',highest?.id],['已晋升',D.state.promoted_record],['已归档版本',D.state.archived_release],['已提交（有收据）',D.state.submitted_release]].map(([label,value])=>`<div class="card">${label}<br><b>${esc(value||'未设置')}</b></div>`).join('');
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
