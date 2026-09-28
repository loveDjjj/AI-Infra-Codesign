"""正式合格结果自动审计，审计不会晋升或上传。"""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from codesign_lab.search.pipeline import Pipeline, select_jobs
from codesign_lab.search.scheduler import atomic_json
from codesign_lab.evaluation.audit import execute


class AutoAuditChecks(unittest.TestCase):
    def complete_full(self, root, eligible, disabled=False):
        candidate = root/'candidate';candidate.mkdir()
        atomic_json(candidate/'config.json', {})
        report = root/'grade.json'
        atomic_json(report, {'eligible': eligible, 'experimental_score': 50000,
                            'cases': {'M1_P1': {'cycles': 1, 'resource_stats': {'large': True}}}})
        controller = Pipeline.__new__(Pipeline)
        controller.done=[];controller.out=root;controller.python='python'
        controller.args=SimpleNamespace(no_auto_audit=disabled)
        controller.enqueue=Mock()
        controller.triggers=Mock()
        controller.pool=Mock()
        with patch('codesign_lab.search.pipeline.read', return_value=[]), \
             patch('codesign_lab.search.pipeline.append') as append:
            controller.completed({'stage':'full','key':'full-pair', 'candidate':candidate,
                                  'report':report}, {'status':'completed','wall_seconds':3})
        return controller, append.call_args[0][0]

    def test_eligible_full_releases_audit_and_keeps_ledger_light(self):
        with tempfile.TemporaryDirectory() as directory:
            controller, record=self.complete_full(Path(directory), True)
            self.assertEqual(controller.enqueue.call_args[0][0]['stage'], 'audit')
            self.assertNotIn('resource_stats', record['cases']['M1_P1'])
            self.assertFalse(record['audited'])

    def test_ineligible_or_disabled_does_not_audit(self):
        for eligible, disabled in [(False,False),(True,True)]:
            with tempfile.TemporaryDirectory() as directory:
                controller,_=self.complete_full(Path(directory),eligible,disabled)
                controller.enqueue.assert_not_called()

    def test_audit_uses_reserved_capacity(self):
        task={'stage':'audit','memory_bytes':1}
        self.assertEqual(select_jobs([task],[{'stage':'case','memory_bytes':1}],0,2,1,10,10),[task])

    def test_audit_failure_prevents_retention(self):
        with patch('codesign_lab.evaluation.audit.audit',side_effect=ValueError('bad evidence')), \
             patch('codesign_lab.evaluation.audit.retain_release') as retain:
            with self.assertRaises(ValueError):execute('record')
            retain.assert_not_called()

    def test_audit_retains_evidence_without_promote(self):
        with patch('codesign_lab.evaluation.audit.audit',return_value={'id':'record'}) as audit, \
             patch('codesign_lab.evaluation.audit.retain_release',return_value={'id':'record','audited':True}) as retain:
            result=execute('record')
            audit.assert_called_once_with('record')
            retain.assert_called_once_with({'id':'record'})
            self.assertTrue(result['audited'])
