import unittest,tempfile,json
from pathlib import Path
from unittest.mock import patch
from codesign_lab.config import ROOT,verify_official,digest
from codesign_lab.evaluation.profile import summarize,operator_spans
from codesign_lab.evaluation.cache import ResultCache
from codesign_lab.search.space import candidates
from codesign_lab.search.prune import reject

class Contracts(unittest.TestCase):
    def test_current_outputs_reproduce_anchor(self):
        from codesign_lab.build import build
        with tempfile.TemporaryDirectory() as directory:
            result=build(ROOT/'configs/best.yaml',Path(directory)/'candidate',ROOT/'data/releases/joint28')
            self.assertTrue(result['verified'])
    def test_official_files_unchanged(self):self.assertIn('baseline_sha256',verify_official())
    def test_cache_separates_seed_engine_and_corruption(self):
        with tempfile.TemporaryDirectory() as directory:
            cache=ResultCache(directory,{'release':'a'});count=[]
            compute=lambda:count.append(1) or {'passed':True}
            cache.call('functional',{'seed':7},compute)
            self.assertTrue(cache.call('functional',{'seed':7},compute)[1])
            self.assertFalse(cache.call('functional',{'seed':123},compute)[1])
            self.assertFalse(ResultCache(directory,{'release':'b'}).call('functional',{'seed':7},compute)[1])
            for path in Path(directory).rglob('*.json'):path.write_text('bad')
            self.assertFalse(cache.call('functional',{'seed':7},compute)[1])
    def test_resource_entity_capacity_not_summed(self):
        result=summarize({'cycles':100,'resource_stats':{'sms':{'0':{'busy_cycles':{'dma':150,'tc':20},'entity_count':{'dma':2,'tc':1}}}}})
        self.assertEqual(result['pressure_leader']['utilization'],.75)
        self.assertEqual(result['wait_estimate']['fraction'],.25)
        self.assertIn('not measured',result['wait_estimate']['classification'])
    def test_loop_expanded_events_map_to_source_line(self):
        from codesign_lab.config import bootstrap
        bootstrap()
        program='FOR '+json.dumps({'var':'i','start':0,'stop':2,'step':1})+'\nLD '+json.dumps({'event':'e{i}'})+'\nEND.FOR {}\n'
        spans=operator_spans(program,[{'source_line_start':2,'source_line_end':2,'operator':'test'}],{'e0':{'issue':1,'finish':3},'e1':{'issue':4,'finish':9}})
        self.assertEqual(spans[0]['events'],2);self.assertEqual(spans[0]['span_cycles'],8)
    def test_phase_names_cover_p1_prompt_and_step(self):
        from codesign_lab.evaluation.profile import stage_labels
        self.assertEqual(stage_labels({'name':'pbothl0w2'})['phase'],'prompt')
        self.assertEqual(stage_labels({'name':'sbothl2qkv'})['decode_step'],0)
        self.assertEqual(stage_labels({'name':'d7l1w2'})['decode_step'],7)
    def test_ledger_parallel_writers_preserve_all_records(self):
        import subprocess,sys,os
        with tempfile.TemporaryDirectory() as directory:
            target=Path(directory)/'experiments.jsonl'
            code="from pathlib import Path;import sys;import codesign_lab.records as r;r.LEDGER=Path(sys.argv[1]);r.append({'id':sys.argv[2]})"
            children=[subprocess.Popen([sys.executable,'-c',code,str(target),str(index)]) for index in range(4)]
            for child in children:self.assertEqual(child.wait(),0)
            self.assertEqual({json.loads(line)['id'] for line in target.read_text().splitlines()},{str(index) for index in range(4)})
    def test_migrated_project_has_no_old_runtime_references(self):
        from codesign_lab.records import read
        for record in read():
            for key in ['report','audit_path','reproduction_evidence','candidate']:
                value=record.get(key)
                if value:
                    self.assertFalse(Path(value).is_absolute(),(record['id'],key,value))
                    self.assertTrue((ROOT/value).exists(),(record['id'],key,value))
        self.assertFalse(any(path.is_symlink() for path in (ROOT/'data').rglob('*')))
    def test_workspace_outputs_are_grouped_by_purpose(self):
        from codesign_lab.config import workspace_output
        self.assertEqual(workspace_output(ROOT/'workspace/check.json','evaluations'),ROOT/'workspace/evaluations/check.json')
        self.assertEqual(workspace_output(ROOT/'workspace/builds/name','builds'),ROOT/'workspace/builds/name')
    def test_search_deduplicates_and_hard_prunes_area(self):
        from codesign_lab.config import load
        base=load(ROOT/'configs/best.yaml')
        self.assertEqual(len(list(candidates(base,{'hardware.cache_mib':[0,0]},10))),1)
        base['hardware']['sm_count']=32
        self.assertIn('area',reject(base))
        base['hardware']['cache_mib']=1000
        self.assertIn('invalid hardware',reject(base))
    def test_clean_never_traverses_long_term_data(self):
        from codesign_lab.release import clean
        self.assertTrue(all('/workspace/' in record['path'] for record in clean()))
        self.assertFalse(any('/data/' in record['path'] for record in clean()))

if __name__=='__main__':unittest.main()
