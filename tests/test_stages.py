"""阶段依赖必须绑定真实输入、工具与种子。"""
import copy
import unittest
from pathlib import Path
from codesign_lab.evaluation.stages import functional_evidence
from codesign_lab.evaluation.pipeline import task


class StageChecks(unittest.TestCase):
    def setUp(self):
        self.report = {'mode': 'functional', 'hardware': {'x': 1},
            'program_sha256': {'M2_D1': 'asm'}, 'provenance': {'engine': 'frozen'},
            'functional_seeds': [7,123], 'completed_without_error': True,
            'cases': {'M2_D1': {'functional_passed': True, 'race_validation_passed': True,
                              'functional': [{'passed': True}, {'passed': True}]}}}

    def validate(self, report):
        return functional_evidence(report, 'M2_D1', {'x':1}, 'asm', {'engine':'frozen'}, [7,123])

    def test_passed_report_returns_independent_copy(self):
        result = self.validate(self.report)
        result['functional'][0]['passed'] = False
        self.assertTrue(self.report['cases']['M2_D1']['functional'][0]['passed'])

    def test_wrong_identity_and_missing_seeds_rejected(self):
        for patch in [{'hardware': {'x':2}}, {'program_sha256': {'M2_D1':'other'}},
                      {'provenance': {'engine':'other'}}, {'functional_seeds':[7]},
                      {'mode':'estimate'}, {'completed_without_error':False}]:
            with self.assertRaises(ValueError):
                self.validate(self.report | patch)

    def test_failed_seed_or_race_rejected(self):
        for field in ['functional_passed', 'race_validation_passed']:
            report = copy.deepcopy(self.report)
            report['cases']['M2_D1'][field] = False
            with self.assertRaises(ValueError): self.validate(report)
        report = copy.deepcopy(self.report)
        report['cases']['M2_D1']['functional'][1]['passed'] = False
        with self.assertRaises(ValueError): self.validate(report)

    def test_task_carries_stage_and_dependency(self):
        job = task('python', Path('/candidate'), 'M2_D1', [7,123], Path('/report'), 'key', 1,
                   mode='estimate', functional_report=Path('/functional'))
        self.assertIn('--functional-report', job['command'])
        self.assertEqual(job['command'][job['command'].index('--mode')+1], 'estimate')
        with self.assertRaises(ValueError):
            task('python', Path('/candidate'), 'M2_D1', [7], Path('/report'), 'key', 1,
                 mode='functional', functional_report=Path('/functional'))


    def test_unrelated_case_change_keeps_case_dependency_valid(self):
        report = copy.deepcopy(self.report)
        report['provenance']['program_sha256'] = {'M2_D1':'asm','M1_P1':'old'}
        expected = {'engine':'frozen','program_sha256':{'M2_D1':'asm','M1_P1':'new'}}
        result = functional_evidence(report,'M2_D1',{'x':1},'asm',expected,[7,123])
        self.assertTrue(result['functional_passed'])
        expected['program_sha256']['M2_D1'] = 'changed'
        with self.assertRaises(ValueError):
            functional_evidence(report,'M2_D1',{'x':1},'asm',expected,[7,123])
