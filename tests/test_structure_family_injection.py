"""验证结构研究候选直接进入原批次，且源码身份变化会阻止后续构建。"""
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from codesign_lab.search import implementation, pipeline, targets


class StructureFamilyInjectionChecks(unittest.TestCase):
    def test_late_structure_falls_back_to_new_campaign(self):
        with tempfile.TemporaryDirectory() as directory:
            campaign = Path(directory)
            state = {'target_limit': 3, 'targets': {},
                'budget': {'deadline': time.time() + 1200,
                'limits': {'case': 3}, 'reservations': {}}}
            (campaign / 'state.json').write_text(json.dumps(state))
            self.assertTrue(implementation.same_campaign_room(campaign))
            state['budget']['reservations'] = {'one': {'kind': 'case', 'reused': False}}
            (campaign / 'state.json').write_text(json.dumps(state))
            self.assertFalse(implementation.same_campaign_room(campaign))
            state['budget']['reservations'] = {}
            state['budget']['deadline'] = time.time() + 120
            (campaign / 'state.json').write_text(json.dumps(state))
            self.assertFalse(implementation.same_campaign_room(campaign))

    def test_research_record_injects_same_campaign_and_pins_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'workspace/implementation-loop/campaign/first/source'
            (source / 'src').mkdir(parents=True)
            (source / 'src/generator.py').write_text('version = 1\n')
            for base in (root, source):
                official = base / 'vendor/official/isolation-manifest.json'
                official.parent.mkdir(parents=True)
                official.write_text('{"version": 1}\n')
            campaign = root / 'workspace/pipeline/campaign'
            campaign.mkdir(parents=True)
            record = {'id': 'research-first', 'config': {'hardware': {}, 'programs': {
                'M2_D1': {'config': {'w2_load_group_size': 16}}}},
                'source_root': str(source.relative_to(root)),
                'source_sha256': implementation.snapshot_epoch(source),
                'research_admission': True}
            pool = targets.TargetPool(campaign, 'main-epoch', max_proposals=8)
            item = {'id': 'first', 'lane': 'd1_decode', 'case': 'M2_D1'}
            with patch.object(targets, 'ROOT', root), patch.object(implementation, 'ROOT', root), \
                 patch.object(targets, 'read', return_value=[record]):
                request_id = implementation.inject_research_target(campaign, item, record['id'])
                receipts = pool.consume()
                self.assertEqual(receipts, [{'status': 'accepted',
                    'target_id': 'structure-first'}])
                self.assertEqual(pool.state['targets']['structure-first']['definition']['execution_root'],
                    str(source))
                self.assertEqual(request_id, 'structure-first')
                (source / 'src/generator.py').write_text('version = 2\n')
                raw = dict(pool.state['targets']['structure-first']['definition'])
                raw.pop('execution_root'); raw.pop('execution_sha256')
                with self.assertRaisesRegex(ValueError, '来源身份'):
                    targets.validate_target(raw, 'main-epoch', [record], max_trials=8)

    def test_completed_structure_consumes_target_and_marks_injected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign = root / 'workspace/pipeline/campaign'
            pool = targets.TargetPool(campaign, 'main-epoch', max_proposals=8)
            state_path = root / 'workspace/implementation-loop/campaign/first/state.json'
            state_path.parent.mkdir(parents=True)
            state_path.write_text(json.dumps({'status': 'TARGET_QUEUED',
                'target_request_id': 'structure-first', 'official_score': 51000,
                'proposal': {'case': 'M2_D1'}}))
            pool.state['requests']['structure-first'] = {'status': 'accepted',
                'target_id': 'structure-first'}
            pool.save()
            controller = pipeline.Pipeline.__new__(pipeline.Pipeline)
            controller.out, controller.pool = campaign, pool
            controller.triggers, controller.budget = Mock(), Mock()
            controller.done, controller.initial_score = [], 50000
            with patch.object(pipeline, 'ROOT', root):
                controller.completed({'stage': 'implementation', 'proposal_id': 'first'},
                    {'status': 'completed'})
            self.assertEqual(json.loads(state_path.read_text())['status'], 'TARGET_INJECTED')
            self.assertEqual(controller.initial_score, 51000)
            self.assertEqual(controller.triggers.observe.call_args.args[1]['target_id'],
                             'structure-first')
