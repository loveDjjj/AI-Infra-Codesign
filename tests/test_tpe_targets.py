"""TPE 动态目标仅开放已实现的固定硬件单案空间。"""
import unittest
from unittest.mock import patch
from codesign_lab.search.targets import validate_target


class TPETargetChecks(unittest.TestCase):
    def target(self):
        return {'schema_version':1,'target_id':'t','lane':'d1_decode','hypothesis':'检验TPE目标边界',
            'base_record':'base','source_epoch':'epoch','cases':['M2_D1'],
            'variables':{'programs.M2_D1.config.w2_load_group_size':[4,8,16]},
            'sampler':'tpe','max_trials':3,'priority':.5,'evidence_ids':['base']}
    records=[{'id':'base','config':{'hardware':{},'programs':{}}}]

    def test_prior_ids_must_be_unique_existing_tpe_records(self):
        record={'id':'prior','config':{},'report':'workspace/prior.json'}
        with patch('pathlib.Path.is_file',return_value=True):
            target,_=validate_target(self.target()|{'prior_record_ids':['prior']},'epoch',self.records+[record])
            self.assertEqual(target['prior_record_ids'],['prior'])
            for ids in [['missing'],['prior','prior'],True]:
                with self.assertRaises(ValueError):validate_target(self.target()|{'prior_record_ids':ids},'epoch',self.records+[record])
            with self.assertRaises(ValueError):validate_target(self.target()|{'sampler':'random','prior_record_ids':['prior']},'epoch',self.records+[record])

    def test_default_window_is_one(self):
        with patch('pathlib.Path.is_file',return_value=True):
            target,_=validate_target(self.target(),'epoch',self.records)
        self.assertEqual(target['max_inflight'],1)

    def test_hardware_or_multi_case_is_rejected(self):
        for target in [self.target()|{'cases':['M1_P1','M2_D1']},
                self.target()|{'cases':['M1_P1','M2_D1'],'variables':{'hardware.cache_mib':[0,1]}}]:
            with self.assertRaisesRegex(ValueError,'固定硬件单案例'):validate_target(target,'epoch',self.records)

    def test_missing_backend_is_rejected(self):
        with patch('pathlib.Path.is_file',return_value=False):
            with self.assertRaisesRegex(ValueError,'尚未安装'):validate_target(self.target(),'epoch',self.records)

if __name__=='__main__':unittest.main()
