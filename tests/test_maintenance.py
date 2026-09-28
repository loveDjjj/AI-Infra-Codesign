"""验证轻资产清理的保留边界和决策事实。"""
import json
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

from codesign_lab.maintenance import apply,plan
from codesign_lab.records import compact_record
from codesign_lab.search.decisions import compact_observation


class MaintenanceChecks(unittest.TestCase):
    def test_compact_preserves_metrics_and_pressure(self):
        record={'id':'trial','cases':{'M1_P1':{'timing':{'cycles':100,
                'peak_window_power_w':19,'resource_stats':{'huge':[1,2]}}}},
                'profile':{'M1_P1':{'cycles':100,'resources':[
                    {'resource':'dma','utilization':.3},{'resource':'rf','utilization':.8}],
                    'pressure_leader':{'resource':'rf'},'stage_status':'推算'}}}
        result=compact_record(record)
        self.assertEqual(result['cases']['M1_P1']['timing']['cycles'],100)
        self.assertNotIn('resource_stats',result['cases']['M1_P1']['timing'])
        self.assertEqual(result['profile']['M1_P1']['top_resources'][0]['resource'],'rf')
        self.assertEqual(compact_observation({'timing':{'cycles':1,'resource_stats':{'large':True}}})['timing'],{'cycles':1})

    def test_apply_keeps_best_and_course_anchors(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for name in ('data/releases/top','data/releases/joint28','data/releases/joint24',
                         'data/releases/old','data/evidence/old','data/agent-trace',
                         'workspace/pipeline/old','workspace/ai-sessions','workspace/search-env'):
                (root/name).mkdir(parents=True)
            (root/'data/releases/old/local-grade.json').write_text('old')
            (root/'data/releases/top/local-grade.json').write_text('best')
            (root/'data/agent-trace/trace.jsonl').write_text('trace')
            (root/'workspace/pipeline/old/report.json').write_text('old')
            (root/'workspace/ai-sessions/global.json').write_text('session')
            (root/'workspace/search-env/python').write_text('runtime')
            (root/'data/state.json').write_text(json.dumps({'promoted_record':'baseline','rollback_record':'rollback'}))
            records=[{'id':'top','scope':'full','score':10,'audited':True,'eligible':True,
                      'report':'data/releases/top/local-grade.json'},
                     {'id':'baseline','scope':'full','report':'data/releases/joint28/local-grade.json'},
                     {'id':'rollback','scope':'full','report':'data/releases/joint24/local-grade.json'},
                     {'id':'old','scope':'full','score':9,'audited':True,'eligible':True,
                      'cases':{'M1_P1':{'timing':{'cycles':42,'resource_stats':{'big':True}}}},
                      'report':'data/releases/old/local-grade.json',
                      'candidate':'workspace/pipeline/old','source_root':'workspace/pipeline/old',
                      'source_snapshot':'workspace/pipeline/old',
                      'profile_evidence':{'M1_P1':{'path':'workspace/pipeline/old/report.json'}},
                      'historical_locations':{'report':'workspace/deleted.json'}}]
            (root/'data/experiments.jsonl').write_text(''.join(json.dumps(row)+'\n' for row in records))
            (root/'data/decisions.jsonl').write_text(json.dumps({'id':'decision',
                'evidence_snapshot':{'a':{'timing':{'cycles':42,'resource_stats':{'big':True}}}}})+'\n')
            with patch('codesign_lab.maintenance.active_search',return_value=[]), \
                 patch('codesign_lab.maintenance.locked',return_value=nullcontext()):
                preview=plan(root)
                self.assertIn('data/releases/old',{item['path'] for item in preview['paths']})
                self.assertNotIn('data/releases/top',{item['path'] for item in preview['paths']})
                apply(root)
            self.assertFalse((root/'data/releases/old').exists())
            self.assertTrue((root/'data/releases/top/local-grade.json').exists())
            self.assertTrue((root/'data/releases/joint24').exists())
            self.assertTrue((root/'data/agent-trace/trace.jsonl').exists())
            self.assertTrue((root/'workspace/ai-sessions/global.json').exists())
            self.assertTrue((root/'workspace/search-env/python').exists())
            history=[json.loads(line) for line in (root/'data/experiments.jsonl').read_text().splitlines()]
            old=history[-1]
            self.assertEqual(old['cases']['M1_P1']['timing']['cycles'],42)
            self.assertNotIn('report',old)
            self.assertNotIn('candidate',old)
            self.assertNotIn('source_root',old)
            self.assertIs(old['source_available'],False)
            self.assertNotIn('source_snapshot',old)
            self.assertNotIn('profile_evidence',old)
            self.assertNotIn('historical_locations',old)
            self.assertEqual(old['reproduction'],'record_only')
            decision=json.loads((root/'data/decisions.jsonl').read_text())
            self.assertEqual(decision['evidence_snapshot']['a']['timing'],{'cycles':42})


if __name__=='__main__':unittest.main()
