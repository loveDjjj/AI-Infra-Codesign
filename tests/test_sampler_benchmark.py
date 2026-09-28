"""对照工具预算门槛与产物去重；夹具不执行芯片模拟。"""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from codesign_lab.config import ROOT,load
from codesign_lab.search.benchmark import plan,preflight
from codesign_lab.search.scheduler import atomic_json


class SamplerBenchmarkChecks(unittest.TestCase):
    def test_small_budget_or_single_seed_is_not_algorithm_comparison(self):
        config=load(ROOT/'configs/benchmark-samplers.yaml')
        with tempfile.TemporaryDirectory(dir=ROOT/'workspace/pipeline') as directory:
            out=Path(directory);path=out/'config.json'
            for changes in [{'trials':12},{'seeds':[7]},{'trials':36}]:
                atomic_json(path,config|changes)
                with self.assertRaises(ValueError):plan(path,out)

    def test_nominal_space_does_not_replace_artifact_uniqueness(self):
        with tempfile.TemporaryDirectory() as directory:
            out=Path(directory);manifest={'base_config':{'x':0},'source_epoch':'source','official':{},
                'nominal_space':3,'settings':{'variables':{'x':[1,2,3]},'workers':4,
                'timeout_seconds':30,'wall_seconds':100,'case':'M2_D1','trials':2}}
            def builds(jobs,*args,**kwargs):
                rows=[]
                for job in jobs:
                    candidate=Path(job['candidate']);(candidate/'programs').mkdir(parents=True)
                    (candidate/'hardware.json').write_text('{}')
                    (candidate/'programs/M2_D1.asm').write_text('same')
                    rows.append({'key':job['key'],'status':'completed'})
                return {'jobs':rows}
            with patch('codesign_lab.search.benchmark.runtime',return_value=('python',{})), \
                 patch('codesign_lab.search.benchmark.run',side_effect=builds), \
                 patch('codesign_lab.search.benchmark.epoch',return_value='source'), \
                 patch('codesign_lab.search.benchmark.verify_official',return_value={}):
                report=preflight(manifest,out)
            self.assertEqual(report['nominal'],3)
            self.assertEqual(report['unique_artifacts'],1)
            self.assertFalse(report['comparison_ready'])
            self.assertEqual(report['simulator_calls'],0)

if __name__=='__main__':unittest.main()
