"""窗口按未完成候选计算，完成后补位且不提前关闭目标。"""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
from codesign_lab.search.pipeline import Pipeline


class ProposalWindowChecks(unittest.TestCase):
    def test_one_candidate_at_a_time_and_finite_space_closes(self):
        with tempfile.TemporaryDirectory() as directory:
            entry={'status':'QUEUED','trials_launched':0,'trials_completed':0,
                'base_config':{'value':0},'definition':{'variables':{'value':[1,2]},
                    'cases':['M2_D1'],'priority':.5,'max_trials':3,'max_inflight':1}}
            p=Pipeline.__new__(Pipeline);p.out=Path(directory);p.python='python'
            p.pool=SimpleNamespace(state={'targets':{'t':entry},'jobs':{}},consume=Mock(),save=Mock(),max_proposals=5)
            p.triggers=SimpleNamespace(poll=Mock(return_value=[]))
            p.enqueue=Mock();p.persist_job=Mock();p.pending=[];p.seen=set()
            with patch('codesign_lab.search.pipeline.reject',return_value=None):
                p.process_targets();self.assertEqual(len(entry['candidates']),1)
                p.process_targets();self.assertEqual(len(entry['candidates']),1)
                first=next(iter(entry['candidates'].values()));first['status']='OBSERVED'
                p.process_targets();self.assertEqual(len(entry['candidates']),2)
                self.assertEqual(entry['status'],'ACTIVE')
                for candidate in entry['candidates'].values():candidate['status']='OBSERVED'
                p.process_targets()
                self.assertEqual(entry['status'],'DONE')
                self.assertTrue(entry['proposal_exhausted'])
                self.assertEqual(entry['trials_launched'],2)

    def test_invalid_window_rejected_before_target_is_queued(self):
        from codesign_lab.search.targets import validate_target
        target={'schema_version':1,'target_id':'window','lane':'d1_decode','hypothesis':'检查窗口约束',
            'base_record':'base','source_epoch':'epoch','cases':['M2_D1'],
            'variables':{'programs.M2_D1.config.w2_load_group_size':[4,8]},'sampler':'random',
            'max_trials':2,'priority':.5,'evidence_ids':['base']}
        records=[{'id':'base','config':{'hardware':{},'programs':{}}}]
        self.assertEqual(validate_target(target|{'max_inflight':1},'epoch',records)[0]['max_inflight'],1)
        for window in [True,0,-1,3,'1']:
            with self.assertRaises(ValueError):validate_target(target|{'max_inflight':window},'epoch',records)
