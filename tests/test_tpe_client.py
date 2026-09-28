"""真实跨解释器协议与丢回答恢复，不调用芯片模拟器。"""
import tempfile
import unittest
from pathlib import Path
from codesign_lab.config import ROOT
from codesign_lab.search.tpe_client import TPEClient


@unittest.skipUnless((ROOT/'workspace/search-env/bin/python').is_file(),'需要独立搜索环境')
class TPEClientChecks(unittest.TestCase):
    scope={'source_epoch':'client-test','hardware_hash':'hw','case':'M2_D1'}

    def test_lost_answer_replays_same_trial(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'workspace/pipeline') as directory:
            client=TPEClient(directory,{'x':[1,2,3]},self.scope,seed=42,startup_trials=2)
            first=client.ask('ask-1')
            (Path(directory)/'answers/ask-1.json').unlink()
            restored=TPEClient(directory,{'x':[1,2,3]},self.scope,seed=42,startup_trials=2)
            self.assertEqual(restored.ask('ask-1'),first)
            pending=restored.call('pending','pending-1')
            self.assertEqual(len(pending),1)
            observation={'id':'fact1','case':'M2_D1','hardware_hash':'hw','functional_passed':True,'cycles':100,'peak_power_w':15}
            self.assertEqual(restored.tell('tell-1',first['number'],observation),'eligible')
            (Path(directory)/'answers/tell-1.json').unlink()
            self.assertEqual(restored.tell('tell-1',first['number'],observation),'reused_receipt')

    def test_same_request_id_cannot_change_content(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'workspace/pipeline') as directory:
            first=TPEClient(directory,{'x':[1,2,3]},self.scope,seed=42)
            first.ask('ask-1')
            changed=TPEClient(directory,{'x':[1,2,3]},self.scope,seed=43)
            with self.assertRaises(ValueError):changed.ask('ask-1')
