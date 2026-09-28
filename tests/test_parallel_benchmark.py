"""真实短子进程验证基准队列补位、依赖和失败退出；不运行芯片模拟。"""
import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('parallel_benchmark',Path(__file__).resolve().parents[1]/'scripts/benchmark_parallel_stages.py')
benchmark=importlib.util.module_from_spec(spec);spec.loader.exec_module(benchmark)


class ParallelBenchmarkChecks(unittest.TestCase):
    def test_process_limit_and_stage_release(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);fake=root/'fake.py'
            fake.write_text('''import sys,json,time
from pathlib import Path
args=sys.argv
mode=args[args.index('--mode')+1]
if mode=='estimate':
 dependency=Path(args[args.index('--functional-report')+1]);assert dependency.exists()
time.sleep(.05)
Path(args[args.index('--out')+1]).write_text(json.dumps({'mode':mode}))
''')
            original=subprocess.Popen
            def launch(command,**kwargs):
                return original([sys.executable,str(fake),*command[3:]],**kwargs)
            with patch.object(benchmark.subprocess,'Popen',side_effect=launch), \
                 patch.object(benchmark,'resources',return_value={'cpus':2,'available_memory_bytes':16*1024**3}):
                result=benchmark.run_group([root/'a',root/'b',root/'c'],'M2_D1',2,'split',root/'split',sys.executable,{},5)
            self.assertTrue(result['all_completed'])
            self.assertEqual(result['completed_tasks'],6)
            self.assertEqual(result['max_active'],2)
            self.assertGreater(result['max_sampled_rss_bytes'],0)

    def test_failure_never_releases_estimate(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);original=subprocess.Popen
            def launch(command,**kwargs):return original([sys.executable,'-c','raise SystemExit(1)'],**kwargs)
            with patch.object(benchmark.subprocess,'Popen',side_effect=launch), \
                 patch.object(benchmark,'resources',return_value={'cpus':2,'available_memory_bytes':16*1024**3}):
                result=benchmark.run_group([root/'a'],'M2_D1',2,'split',root/'split',sys.executable,{},5)
            self.assertFalse(result['all_completed'])
            self.assertEqual(list(result['results']),['0-functional'])

    def test_source_change_blocks_publishing_comparison(self):
        from codesign_lab.config import ROOT
        with tempfile.TemporaryDirectory(dir=ROOT/'workspace/pipeline') as directory:
            out=Path(directory)/'benchmark'
            args=['benchmark','--candidate',str(ROOT/'data/releases/joint28'),'--out',str(out)]
            with patch('sys.argv',args),patch.object(benchmark,'source_identity',side_effect=[{'version':1},{'version':2}]), \
                 patch.object(benchmark,'run_group',return_value={'all_completed':True}):
                with self.assertRaisesRegex(RuntimeError,'代码或冻结工具改变'):
                    benchmark.main()
            self.assertTrue((out/'identity.json').exists())
            self.assertFalse((out/'summary.json').exists())
