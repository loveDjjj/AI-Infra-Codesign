"""冻结实现族与根实现共享一份评估队列。"""
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock

from codesign_lab.config import ROOT
from codesign_lab.search.pipeline import Pipeline


class FamilyBrokerChecks(TestCase):
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
