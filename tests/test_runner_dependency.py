"""验证 runner 仅在完整依赖通过时复用竞态证明。"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from codesign_lab.config import ROOT,load
from codesign_lab.evaluation import runner


class RunnerDependencyChecks(unittest.TestCase):
    def fixture(self):
        candidate=ROOT/'data/releases/joint28'
        hardware=runner.Hardware.from_dict(load(candidate/'hardware.json'))
        programs={case:(candidate/'programs'/(case+'.asm')).read_text() for case in ['M1_P1','M2_D1']}
        return {'mode':'functional','hardware':hardware.to_dict(),
            'program_sha256':{case:runner.hashlib.sha256(text.encode()).hexdigest() for case,text in programs.items()},
            'provenance':runner.provenance(hardware,programs),'functional_seeds':[7,123],
            'completed_without_error':True,'cases':{'M2_D1':{'functional_passed':True,
                'race_validation_passed':True,'functional_seconds':1,
                'functional':[{'passed':True},{'passed':True}]}}}

    def test_dependency_avoids_duplicate_race_check(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);dependency=root/'functional.json'
            dependency.write_text(runner.json.dumps(self.fixture()))
            args=['runner',str(ROOT/'data/releases/joint28'),'--mode','estimate','--case','M2_D1',
                '--seed','7','--seed','123','--functional-report',str(dependency),'--out',str(root/'estimate.json')]
            with patch('sys.argv',args),patch.object(runner,'validate_hbm_races') as race, \
                 patch.object(runner,'estimate_case',return_value={'cycles':43523}) as timing:
                self.assertEqual(runner.main(),0)
                race.assert_not_called();timing.assert_called_once()
                self.assertTrue(timing.call_args.kwargs['hbm_races_validated'])
            info=load(root/'estimate.json')['cases']['M2_D1']
            self.assertTrue(info['race_validation_reused_from_dependency'])
            self.assertTrue(info['functional_passed'])

    def test_failed_race_proof_blocks_every_compute_stage(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);dependency=root/'functional.json'
            data=self.fixture();data['cases']['M2_D1']['race_validation_passed']=False
            dependency.write_text(runner.json.dumps(data))
            args=['runner',str(ROOT/'data/releases/joint28'),'--mode','estimate','--case','M2_D1',
                '--seed','7','--seed','123','--functional-report',str(dependency),'--out',str(root/'estimate.json')]
            with patch('sys.argv',args),patch.object(runner,'validate_hbm_races') as race, \
                 patch.object(runner,'estimate_case') as timing:
                with self.assertRaises(ValueError):runner.main()
                race.assert_not_called();timing.assert_not_called()
            self.assertFalse((root/'estimate.json').exists())
