"""统一入口保持检查模式，严格拒绝未知配置与非法路径。"""
import tempfile
import unittest
from pathlib import Path
from codesign_lab.config import ROOT
from codesign_lab.search.settings import arguments
from codesign_lab.search.scheduler import atomic_json
from codesign_lab.search.pipeline import main
from contextlib import redirect_stdout
from io import StringIO


class SettingsChecks(unittest.TestCase):
    def test_global_analysis_mode_is_forwarded(self):
        from codesign_lab.search.settings import arguments
        import json
        from pathlib import Path
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as directory:
            path=Path(directory)/'pipeline.json'
            path.write_text(json.dumps({'schema_version':1,'out':'workspace/pipeline/global-mode-test',
                'search_configs':[],'watch':[],'ai':{'enabled':True,'mode':'global',
                    'model':'gpt-6-astra','reasoning_effort':'medium'}}))
            argv=arguments(path)
            self.assertIn('--ai-enabled',argv)
            self.assertEqual(argv[argv.index('--analysis-mode')+1],'global')
            self.assertEqual(argv[argv.index('--analysis-model')+1],'gpt-6-astra')
            self.assertEqual(argv[argv.index('--analysis-effort')+1],'medium')

    def test_implementation_worker_is_opt_in_and_configured(self):
        argv=self.settings({'implementation':{'enabled':True,'model':'gpt-6-astra',
            'reasoning_effort':'medium','max_proposals':2,'min_score_gain':100}})
        self.assertIn('--implementation-enabled',argv)
        self.assertEqual(argv[argv.index('--implementation-model')+1],'gpt-6-astra')
        for value in ({'enabled':'true'},{'enabled':False,'unknown':1},
                      {'enabled':True,'reasoning_effort':'ultra'}):
            with self.assertRaises(ValueError):self.settings({'implementation':value})

    def settings(self, value):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'settings.json'
            atomic_json(path,dict(schema_version=1,out='workspace/pipeline/test',**value))
            return arguments(path)

    def test_cache_directory_is_explicit_and_workspace_only(self):
        argv=self.settings({"cache_dir":"workspace/pipeline/cold/cache"})
        self.assertEqual(argv[argv.index("--cache-dir")+1],str(ROOT/"workspace/pipeline/cold/cache"))
        for value in ["data/cache", "../outside", True]:
            with self.assertRaises(ValueError):self.settings({"cache_dir":value})

    def test_default_file_is_plan_only_and_ai_disabled(self):
        argv=arguments(ROOT/'configs/pipeline.yaml')
        self.assertNotIn('--execute',argv)
        self.assertNotIn('--ai-enabled',argv)
        with redirect_stdout(StringIO()) as output:main(argv)
        self.assertIn('仅计划',output.getvalue())

    def test_execute_and_resume_are_explicit(self):
        argv=arguments(ROOT/'configs/pipeline.yaml',execute=True,resume=True)
        self.assertIn('--execute',argv);self.assertIn('--resume',argv)

    def test_unknown_or_unimplemented_field_rejected(self):
        for value in [{'search':{'sampler':'tpe'}},{'scheduler':{'unlimited':True}},
                      {'ai':{'enabled':'true'}},{'budget':{'case_calls':True}}]:
            with self.assertRaises(ValueError):self.settings(value)

    def test_escape_search_path_rejected(self):
        with self.assertRaises(ValueError):self.settings({'search_configs':['README.md']})

    def test_memory_fraction_validated_by_main(self):
        argv=self.settings({'scheduler':{'memory_fraction':1.0}})
        with self.assertRaises(SystemExit):main(argv)

    def test_resume_cannot_be_plan_only(self):
        with self.assertRaises(SystemExit):main(arguments(ROOT/'configs/pipeline.yaml',resume=True))
