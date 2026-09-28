"""验证 AI 决策事务、证据校验与重复投递，而非模型质量。"""
import copy
import tempfile
import unittest
from unittest.mock import patch
from codesign_lab.search.targets import TargetPool
from codesign_lab.search.triggers import Triggers
from codesign_lab.search.decisions import apply_decision, schema_check
from codesign_lab.config import ROOT,load


class DecisionChecks(unittest.TestCase):
    def setUp(self):
        self.records=[{'id':'base','config':{'hardware':{},'programs':{}}}]
        self.root=tempfile.TemporaryDirectory();self.addCleanup(self.root.cleanup)
        self.pool=TargetPool(self.root.name,'epoch',max_proposals=2)
        self.pool.state['targets']['old']={'definition':{'lane':'p1_attention','max_trials':0},'status':'DONE'}
        self.triggers=Triggers(self.pool.state)
        request=self.triggers.poll(self.pool.state['targets'],0)[0]
        self.decision={'schema_version':1,'decision_id':request['decision_id'],'lane':'p1_attention',
            'summary':'比较剩余合法查询分块','conclusions':[],'stop_targets':[], 'profile_requests':[],
            'implementation_proposals':[], 'new_targets':[{'schema_version':1,'target_id':'new',
            'lane':'p1_attention','hypothesis':'查询分块影响调度','base_record':'base','source_epoch':'epoch',
            'cases':['M1_P1'],'variables':[{'path':'programs.M1_P1.config.attention_query_tile','values':[8]}],
            'sampler':'enumerate','max_trials':1,'priority':.8,'evidence_ids':['base']}]}

    def test_valid_decision_injects_and_acknowledges_once(self):
        with patch('codesign_lab.search.decisions.read',return_value=self.records),patch('codesign_lab.search.targets.read',return_value=self.records),patch('codesign_lab.search.decisions.append_decision') as record:
            self.assertEqual(apply_decision(self.pool,self.triggers,self.decision)['status'],'accepted')
            self.assertEqual(self.pool.state['targets']['new']['status'],'QUEUED')
            self.assertIsNone(self.triggers.lane('p1_attention')['pending'])
            self.assertEqual(apply_decision(self.pool,self.triggers,self.decision)['status'],'reused')
            self.assertEqual(len(self.pool.state['targets']),2)
            self.assertEqual(record.call_count,2)

    def test_mixed_valid_and_invalid_targets_do_not_partially_apply(self):
        bad=copy.deepcopy(self.decision['new_targets'][0]);bad['target_id']='bad';bad['variables'][0]['values']=[128]
        self.decision['new_targets'].append(bad)
        original=copy.deepcopy(self.pool.state)
        with patch('codesign_lab.search.decisions.read',return_value=self.records):
            with self.assertRaises(ValueError):apply_decision(self.pool,self.triggers,self.decision)
        self.assertEqual(self.pool.state['targets'],original['targets'])
        self.assertIsNotNone(self.triggers.lane('p1_attention')['pending'])

    def test_budget_rejection_rolls_back_all_targets(self):
        self.decision['new_targets'][0]['max_trials']=2
        second=copy.deepcopy(self.decision['new_targets'][0]);second['target_id']='second'
        self.decision['new_targets'].append(second)
        with patch('codesign_lab.search.decisions.read',return_value=self.records),patch('codesign_lab.search.targets.read',return_value=self.records):
            with self.assertRaises(ValueError):apply_decision(self.pool,self.triggers,self.decision)
        self.assertEqual(list(self.pool.state['targets']),['old'])

    def test_schema_and_session_identity_are_required(self):
        schema=load(ROOT/'schemas/ai-decision.schema.json')
        wrong=copy.deepcopy(self.decision);wrong['extra']='command'
        with self.assertRaises(ValueError):schema_check(wrong,schema)
        wrong=copy.deepcopy(self.decision);wrong['decision_id']='a'*64
        with self.assertRaises(ValueError):apply_decision(self.pool,self.triggers,wrong)

    def test_structural_identity_is_validated_before_decision_commit(self):
        self.decision['new_targets'] = []
        self.decision['implementation_proposals'] = [{
            'lane': 'p1_w2', 'transformation_id': 'w2-input-reuse',
            'proposal': '复用输入块', 'evidence_ids': ['base']}]
        with patch('codesign_lab.search.decisions.read',return_value=self.records), \
             patch('codesign_lab.search.decisions.append_decision') as record:
            self.assertEqual(apply_decision(self.pool,self.triggers,self.decision)['status'],'accepted')
            record.assert_called_once()

    def test_invalid_structural_identity_does_not_commit(self):
        self.decision['new_targets'] = []
        self.decision['implementation_proposals'] = [{
            'lane': 'p1_w2', 'transformation_id': '../escape',
            'proposal': '无效 ID', 'evidence_ids': ['base']}]
        with patch('codesign_lab.search.decisions.read',return_value=self.records):
            with self.assertRaisesRegex(ValueError,'结构机制 ID'):
                apply_decision(self.pool,self.triggers,self.decision)
        self.assertIsNotNone(self.triggers.lane('p1_attention')['pending'])
