"""合成报告验证历史身份契约，不代替真实功能或时序测量。"""
import copy
import shutil
import tempfile
import unittest
from pathlib import Path
from codesign_lab.config import ROOT,bootstrap,load,digest
from codesign_lab.search.priors import validated_prior
from codesign_lab.search.scheduler import atomic_json


class PriorChecks(unittest.TestCase):
    def fixture(self,root):
        bootstrap()
        from codesign.challenge.hardware import Hardware
        from codesign.challenge.runner import provenance
        candidate=root/'build';candidate.mkdir();shutil.copytree(ROOT/'data/releases/joint28/programs',candidate/'programs')
        shutil.copyfile(ROOT/'data/releases/joint28/hardware.json',candidate/'hardware.json')
        config=load(ROOT/'configs/best.yaml');config['programs']['M2_D1']['config']['w2_load_group_size']=16
        atomic_json(candidate/'config.json',config)
        hardware=load(candidate/'hardware.json');programs={c:(candidate/'programs'/(c+'.asm')).read_text() for c in ['M1_P1','M2_D1']}
        timing={'cycles':100,'peak_window_power_w':15}
        report={'mode':'estimate','runtime':{'numpy':'2.5.3','python':'3.12.13'},'hardware':hardware,
            'program_sha256':{c:digest(candidate/'programs'/(c+'.asm')) for c in programs},
            'provenance':provenance(Hardware.from_dict(hardware),programs),'functional_seeds':[7,123],
            'completed_without_error':True,'cases':{'M2_D1':{'functional_passed':True,
                'race_validation_passed':True,'functional':[{'passed':True},{'passed':True}],
                'cache_hits':[],'timing':timing}}}
        path=root/'report.json';atomic_json(path,report)
        record={'id':'synthetic-contract','report':str(path.relative_to(ROOT)), 'cases':{'M2_D1':{'timing':timing}}}
        return candidate,path,record,report

    def test_artifact_identity_and_ledger_mismatch(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'workspace/pipeline') as directory:
            candidate,path,record,report=self.fixture(Path(directory))
            variables={'programs.M2_D1.config.w2_load_group_size':[4,8,16]}
            params,observation=validated_prior(record,candidate,'M2_D1',variables)
            self.assertEqual(params[next(iter(variables))],16)
            self.assertEqual(observation['evidence_kind'],'historical_prior')
            wrong=copy.deepcopy(record);wrong['cases']['M2_D1']['timing']['cycles']=1
            with self.assertRaisesRegex(ValueError,'账本'):validated_prior(wrong,candidate,'M2_D1',variables)
            for bad in [report|{'program_sha256':{'M2_D1':'wrong'}},report|{'functional_seeds':[7]},
                        report|{'runtime':{'numpy':'2.2.6','python':'3.12.13'}}]:
                atomic_json(path,bad)
                with self.assertRaises(ValueError):validated_prior(record,candidate,'M2_D1',variables)

    def test_cached_timing_not_imported_as_original_fact(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'workspace/pipeline') as directory:
            candidate,path,record,report=self.fixture(Path(directory))
            report['cases']['M2_D1']['cache_hits']=[{'stage':'timing'}];atomic_json(path,report)
            with self.assertRaisesRegex(ValueError,'缓存请求'):validated_prior(record,candidate,'M2_D1',{'programs.M2_D1.config.w2_load_group_size':[16]})

if __name__=='__main__':unittest.main()
