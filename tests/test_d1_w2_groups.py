"""D1 分组参数必须改变冷加载同步，默认值保持发布程序。"""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from codesign_lab.config import ROOT,load,digest,bootstrap
from codesign_lab.build import build


class D1W2GroupChecks(unittest.TestCase):
    def test_supported_groups_change_only_d1_sync(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);base=load(ROOT/'configs/best.yaml');barriers=[];mmas=[]
            for group in [16,8,4]:
                cfg=copy.deepcopy(base);cfg['programs']['M2_D1']['config']['w2_load_group_size']=group
                path=root/f'group{group}.json';path.write_text(json.dumps(cfg))
                build(path,root/f'group{group}')
                candidate=root/f'group{group}'
                self.assertEqual(digest(candidate/'hardware.json'),digest(ROOT/'data/releases/joint28/hardware.json'))
                self.assertEqual(digest(candidate/'programs/M1_P1.asm'),digest(ROOT/'data/releases/joint28/programs/M1_P1.asm'))
                program=(candidate/'programs/M2_D1.asm').read_text()
                barriers.append(sum(line.startswith('BARRIER ') for line in program.splitlines()))
                mmas.append(sum(line.startswith('MMA.ACC ') for line in program.splitlines()))
                if group==16:self.assertEqual(digest(candidate/'programs/M2_D1.asm'),digest(ROOT/'data/releases/joint28/programs/M2_D1.asm'))
            self.assertLess(barriers[0],barriers[1]);self.assertLess(barriers[1],barriers[2])
            self.assertEqual(mmas,[mmas[0]]*3)

    def test_unsupported_groups_rejected_before_generation(self):
        bootstrap()
        from codesign_lab.codegen.d1.compiler import CompilerConfig
        for group in [True,0,2,3,32,'8']:
            with self.assertRaises(ValueError):CompilerConfig(w2_load_group_size=group)
