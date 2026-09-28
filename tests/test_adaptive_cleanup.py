"""真实隔离后端的预算收缩收尾，不运行芯片模拟器。"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from codesign_lab.config import ROOT,digest,load
from codesign_lab.search.adaptive import complete
from codesign_lab.search.scheduler import atomic_json
from codesign_lab.search.tpe_client import TPEClient


@unittest.skipUnless((ROOT/'workspace/search-env/bin/python').is_file(),'需要独立搜索环境')
class AdaptiveCleanupChecks(unittest.TestCase):
    def test_budget_shrink_closes_created_trial_without_objective(self):
        (ROOT / 'workspace/pipeline').mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT/'workspace/pipeline') as directory:
            out=Path(directory);hardware=out/'builds/base/hardware.json';atomic_json(hardware,{})
            definition={'max_trials':0,'priority':.5,'cases':['M2_D1'],'variables':{'x':[1,2]},'seed':7}
            entry={'status':'ACTIVE','base_key':'base','definition':definition,'candidates':{},
                'adaptive':{'pending_jobs':['sample-ask'],'processed':[],'trials':{},'ask_job':'sample-ask'}}
            p=SimpleNamespace(out=out,python=sys.executable,pool=SimpleNamespace(
                state={'targets':{'t':entry}},source_epoch='cleanup-test',save=Mock()),enqueue=Mock())
            scope={'source_epoch':'cleanup-test','hardware_hash':digest(hardware),'case':'M2_D1'}
            client=TPEClient(out/'samplers/t',definition['variables'],scope,seed=7)
            answer=client.ask('ask-0');spec=out/'sampling/ask.request.json';report=out/'sampling/ask.answer.json'
            atomic_json(spec,{'operation':'ask'})
            atomic_json(report,{'spec_sha256':digest(spec),'request_id':'ask-0','answer':answer})
            job={'key':'sample-ask','target_id':'t','request_spec':str(spec),'report':report,
                 'request_id':'ask-0','operation':'ask'}
            complete(p,job,{'status':'completed'})
            self.assertEqual(entry['candidates'],{})
            cleanup=p.enqueue.call_args.args[0]
            self.assertFalse(cleanup['target_owned'])
            self.assertEqual(cleanup['operation'],'tell')
            env=os.environ.copy();env['PYTHONPATH']=str(ROOT/'src')
            result=subprocess.run(cleanup['command'],env=env,cwd=ROOT,capture_output=True,text=True,timeout=65)
            self.assertEqual(result.returncode,0,result.stderr)
            complete(p,cleanup,{'status':'completed'})
            self.assertTrue(entry['adaptive']['unconsumed_proposals'][0]['told'])
            self.assertEqual(entry['adaptive']['unconsumed_proposals'][0]['outcome'],'no_performance')
            self.assertEqual(client.call('pending','pending-final'),[])
            complete(p,job,{'status':'completed'})
            self.assertEqual(len(entry['adaptive']['unconsumed_proposals']),1)

if __name__=='__main__':unittest.main()
