"""验证主控依赖释放、失败终态与分阶段任务身份。"""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from codesign_lab.config import ROOT,load,digest
from codesign_lab.search.pipeline import Pipeline


class StageSchedulerChecks(unittest.TestCase):
    def controller(self, directory):
        controller=Pipeline.__new__(Pipeline)
        controller.out=Path(directory);controller.python='python'
        controller.manifest={'baseline_sha256':'baseline'}
        controller.pool=SimpleNamespace(save=Mock())
        controller.budget=SimpleNamespace(reused=Mock())
        controller.enqueue=Mock();controller.persist_job=Mock()
        controller.completed=Mock();controller.seen=set()
        return controller

    def test_isolated_cache_applies_to_both_stages(self):
        with tempfile.TemporaryDirectory() as directory:
            controller=self.controller(directory)
            controller.args=SimpleNamespace(cache_dir=ROOT/"workspace/pipeline/isolated/cache")
            job=controller.case_job(ROOT/"data/releases/joint28","M2_D1",[7,123])
            for stage in [job,job["followup"]]:
                command=stage["command"]
                self.assertEqual(command[command.index("--cache-dir")+1],str(controller.args.cache_dir))

    def test_keys_and_commands_bind_stage_dependencies(self):
        with tempfile.TemporaryDirectory() as directory:
            controller=self.controller(directory)
            job=controller.case_job(ROOT/'data/releases/joint28','M2_D1',[7,123])
            self.assertEqual(job['stage'],'functional')
            self.assertNotEqual(job['key'],job['followup']['key'])
            self.assertIn('--functional-report',job['followup']['command'])
            self.assertEqual(job['followup']['stage_mode'],'estimate')

    def test_only_valid_completed_functional_releases_estimate(self):
        with tempfile.TemporaryDirectory() as directory:
            controller=self.controller(directory)
            job=controller.case_job(ROOT/'data/releases/joint28','M2_D1',[7,123])
            # 使用原件结构但不要求测试运行目录长期存在。
            data={'mode':'functional','hardware':load(ROOT/'data/releases/joint28/hardware.json'),
                'program_sha256':{'M2_D1':digest(ROOT/'data/releases/joint28/programs/M2_D1.asm')},
                'provenance':load(ROOT/'data/releases/joint28/local-grade.json')['provenance'],'functional_seeds':[7,123],
                'completed_without_error':True,'cases':{'M2_D1':{'functional_passed':True,
                    'race_validation_passed':True,'functional':[{'passed':True},{'passed':True}],
                    'cache_hits':[{'stage':'functional','seed':7},{'stage':'functional','seed':123}]}}}
            from codesign_lab.search.scheduler import atomic_json
            atomic_json(job['report'],data)
            job['target_owned']=True
            controller.complete_functional(job,{'status':'completed','wall_seconds':1})
            launched=controller.enqueue.call_args.args[0]
            self.assertTrue(launched['target_owned'])
            self.assertEqual(launched['key'],job['followup']['key'])
            controller.budget.reused.assert_called_once_with(job['key'])
            controller.completed.assert_not_called()

    def test_failed_functional_finishes_logical_case_without_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            controller=self.controller(directory)
            job=controller.case_job(ROOT/'data/releases/joint28','M2_D1',[7,123])
            controller.complete_functional(job,{'status':'failed','wall_seconds':1})
            controller.enqueue.assert_not_called()
            result=load(Path(directory)/'jobs'/(job['followup']['key']+'.result.json'))
            self.assertEqual(result['status'],'failed')
            self.assertIn('dependency_error',result)
            self.assertEqual(controller.completed.call_args.args[0]['report'],job['report'])


    def test_spoofed_tool_identity_never_releases_estimate(self):
        with tempfile.TemporaryDirectory() as directory:
            controller=self.controller(directory)
            job=controller.case_job(ROOT/'data/releases/joint28','M2_D1',[7,123])
            data={'mode':'functional','hardware':load(ROOT/'data/releases/joint28/hardware.json'),
                'program_sha256':{'M2_D1':digest(ROOT/'data/releases/joint28/programs/M2_D1.asm')},
                'provenance':{'engine':'spoofed'},'functional_seeds':[7,123],
                'completed_without_error':True,'cases':{'M2_D1':{'functional_passed':True,
                    'race_validation_passed':True,'functional':[{'passed':True},{'passed':True}]}}}
            from codesign_lab.search.scheduler import atomic_json
            atomic_json(job['report'],data)
            controller.complete_functional(job,{'status':'completed','wall_seconds':1})
            controller.enqueue.assert_not_called()
            self.assertIn('工具身份',controller.completed.call_args.args[1]['dependency_error'])
