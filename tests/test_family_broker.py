"""冻结实现族与根实现共享一份评估队列。"""
import json
import hashlib
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from codesign_lab.config import ROOT
from codesign_lab.search.pipeline import Pipeline
from codesign_lab.search.targets import validate_target, epoch


class FamilyBrokerChecks(TestCase):
    def test_global_analysis_receives_eligible_family_base_ids(self):
        controller = Pipeline.__new__(Pipeline)
        controller.out = Path('/tmp/family-campaign')
        controller.pool = SimpleNamespace(state={'targets': {}})
        rows = [{'id': 'family-record', 'campaign': 'family-campaign', 'scope': 'both',
                 'source_root': 'workspace/families/example', 'source_sha256': 'pinned',
                 'config': {'hardware': {}, 'programs': {'M1_P1': {'config': {}}}},
                 'cases': {'M2_D1': {'timing': {'cycles': 42000}, 'functional_passed': True}}}]
        with patch('codesign_lab.search.pipeline.read', return_value=rows):
            snapshot = controller.global_snapshot()
        self.assertEqual(snapshot['available_family_bases'][0]['record_id'], 'family-record')

    def test_dynamic_family_target_pins_recorded_source(self):
        (ROOT / 'workspace/families').mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT / 'workspace/families') as directory:
            source = Path(directory)
            code = source / 'src/code.py'
            code.parent.mkdir()
            code.write_text('value = 1\n')
            (source / 'vendor').mkdir()
            (source / 'vendor/official').symlink_to(ROOT / 'vendor/official', target_is_directory=True)
            files = {'src/code.py': hashlib.sha256(code.read_bytes()).hexdigest()}
            identity = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
            record = {'id': 'family-base', 'source_root': str(source),
                      'source_sha256': identity, 'config': {'hardware': {}, 'programs': {}}}
            target = {'schema_version': 1, 'target_id': 'family-target', 'lane': 'd1_decode',
                      'hypothesis': '验证已有实现族参数', 'base_record': record['id'],
                      'source_epoch': epoch(), 'cases': ['M2_D1'],
                      'variables': {'programs.M2_D1.config.w2_load_group_size': [8]},
                      'sampler': 'enumerate', 'max_trials': 1, 'priority': .8,
                      'evidence_ids': []}
            accepted, _base = validate_target(target, epoch(), [record])
            self.assertEqual(accepted['execution_root'], str(source))
            tpe = dict(target, sampler='tpe')
            accepted_tpe, _base = validate_target(tpe, epoch(), [record])
            self.assertEqual(accepted_tpe['execution_root'], str(source))
            with self.assertRaisesRegex(ValueError, '旧源码先验'):
                validate_target(dict(tpe, prior_record_ids=['family-base']), epoch(), [record])
            code.write_text('value = 2\n')
            with self.assertRaisesRegex(ValueError, '来源身份'):
                validate_target(target, epoch(), [record])

    def test_family_search_enqueues_pinned_build_in_same_queue(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'workspace') as directory:
            root = Path(directory)
            sources = [root / 'family-a', root / 'family-b']
            for source in sources:
                (source / 'configs').mkdir(parents=True)
                (source / 'configs/best.yaml').write_text((ROOT / 'configs/best.yaml').read_text())
            spec = root / 'search.json'
            spec.write_text(json.dumps({'base': 'configs/best.yaml', 'max_candidates': 0,
                'variables': {'programs.M1_P1.config.w1_preload_k': [32]},
                'cases': ['M1_P1']}))
            controller = Pipeline.__new__(Pipeline)
            controller.args = SimpleNamespace(config=[], family_config=[(str(source), str(spec))
                                                                   for source in sources])
            controller.out = root / 'queue'
            controller.python = '/test/python'
            controller.pending = []
            controller.family_sources = {str(source): {'src/code.py': source.name}
                                         for source in sources}
            controller.enqueue = Mock()
            controller.prepare()
            jobs = [call.args[0] for call in controller.enqueue.call_args_list]
            self.assertEqual({job['source_root'] for job in jobs}, {str(source) for source in sources})
            self.assertEqual(len({job['key'] for job in jobs}), 2)
            for job in jobs:
                self.assertEqual(job['cases'], ['M1_P1'])
                self.assertIn('codesign_lab.search.family_build', job['command'])
            self.assertTrue((controller.out / 'sources').is_dir())
