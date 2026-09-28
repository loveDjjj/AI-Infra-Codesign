"""TPE 监督回调、目标订阅和未完成反馈边界。"""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from codesign_lab.search.adaptive import complete,request_job
from codesign_lab.search.pipeline import Pipeline
from codesign_lab.search.target_control import priorities
from codesign_lab.search.scheduler import atomic_json
from codesign_lab.config import digest


class AdaptiveChecks(unittest.TestCase):
    def controller(self,directory):
        entry={'status':'ACTIVE','definition':{'priority':.8,'max_trials':1},'base_key':'base',
            'adaptive':{'pending_jobs':['sample-a'],'processed':[], 'trials':{'c':{'number':2}}},
            'candidates':{}}
        p=Pipeline.__new__(Pipeline);p.out=Path(directory);p.done=[];p.pending=[];p.python='python'
        p.persist_job=Mock();p.seen=set()
        p.pool=SimpleNamespace(state={'targets':{'t':entry}},save=Mock())
        return p,entry

    def test_immediate_replay_does_not_restore_pending_ask(self):
        with tempfile.TemporaryDirectory() as directory:
            p,entry=self.controller(directory)
            entry['definition'].update(variables={'x':[1]},cases=['M2_D1'],seed=1)
            p.pool.source_epoch='epoch'
            atomic_json(p.out/'builds/base/hardware.json',{})
            def replay(job):
                self.assertEqual(entry['adaptive']['ask_job'],job['key'])
                entry['adaptive']['ask_job']=None
            p.enqueue=Mock(side_effect=replay)
            request_job(p,'t',entry,'ask','ask-0')
            self.assertIsNone(entry['adaptive']['ask_job'])

    def test_invalid_answer_isolates_target(self):
        with tempfile.TemporaryDirectory() as directory:
            p,entry=self.controller(directory)
            spec=p.out/'request.json';report=p.out/'answer.json'
            atomic_json(spec,{'fields':{'number':2}})
            atomic_json(report,{'spec_sha256':'wrong','request_id':'r','answer':{}})
            job={'key':'sample-a','stage':'sample','target_id':'t','request_spec':str(spec),
                 'report':report,'request_id':'r','operation':'tell'}
            p.completed(job,{'status':'completed'})
            self.assertEqual(entry['status'],'FAILED')
            self.assertEqual(entry['adaptive']['pending_jobs'],[])
            self.assertEqual(len(p.done),1)

    def test_tell_completion_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            p,entry=self.controller(directory)
            spec=p.out/'request.json';report=p.out/'answer.json'
            atomic_json(spec,{'fields':{'number':2}})
            atomic_json(report,{'spec_sha256':digest(spec),'request_id':'r','answer':{'status':'eligible'}})
            job={'key':'sample-a','target_id':'t','request_spec':str(spec),'report':report,
                 'request_id':'r','operation':'tell'}
            complete(p,job,{'status':'completed'});complete(p,job,{'status':'completed'})
            self.assertTrue(entry['adaptive']['trials']['c']['told'])
            self.assertEqual(entry['adaptive']['processed'],['sample-a'])

    def test_sampling_subscription_survives_cancel_sweep(self):
        with tempfile.TemporaryDirectory() as directory:
            p,entry=self.controller(directory)
            p.pending=[{'key':'sample-a','target_owned':True},{'key':'build-base','target_owned':True}]
            p.cancel_stopped_targets()
            self.assertEqual(len(p.pending),2)
            self.assertEqual(priorities(p.pool.state['targets']),{'sample-a':.8,'build-base':.8})

if __name__=='__main__':unittest.main()
