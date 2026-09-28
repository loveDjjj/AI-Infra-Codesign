"""隔离搜索环境中的真实 Optuna 持久后端检查，不伪造芯片评测。"""
import importlib.util
import tempfile
import unittest
from codesign_lab.search.tpe import OptunaSampler


@unittest.skipUnless(importlib.util.find_spec('optuna'),'需要独立搜索环境')
class TPEBackendChecks(unittest.TestCase):
    scope={'source_epoch':'test-source','hardware_hash':'hw','case':'M2_D1'}

    def observation(self,**changes):
        return {'id':'observed','case':'M2_D1','hardware_hash':'hw','functional_passed':True,
                'cycles':100,'peak_power_w':15}|changes

    def test_imported_prior_is_idempotent_and_excluded_from_proposals(self):
        with tempfile.TemporaryDirectory() as directory:
            sampler=OptunaSampler(directory,{'x':[1,2,3]},self.scope)
            observation=self.observation(evidence_kind='historical_prior',report_sha256='a'*64)
            first=sampler.import_observation({'x':2},observation)
            self.assertEqual(first['status'],'imported_prior')
            self.assertEqual(sampler.import_observation({'x':2},observation)['status'],'reused_prior')
            trials=sampler.study(0).get_trials()
            self.assertEqual(len(trials),1);self.assertEqual(trials[0].value,100)
            self.assertTrue(trials[0].user_attrs['historical_prior'])
            proposal=sampler.ask()
            self.assertNotEqual(proposal['params']['x'],2)
            self.assertEqual(proposal['completed_observations_at_ask'],1)
            with self.assertRaises(ValueError):sampler.import_observation({'x':2},observation|{'cycles':101})

    def test_invalid_prior_does_not_create_trial(self):
        with tempfile.TemporaryDirectory() as directory:
            sampler=OptunaSampler(directory,{'x':[1,2,3]},self.scope)
            observation=self.observation(evidence_kind='historical_prior',report_sha256='a'*64)
            for bad in [observation|{'cache_reused':True},observation|{'hardware_hash':'other'},observation|{'cycles':None}]:
                with self.assertRaises(ValueError):sampler.import_observation({'x':2},bad)
            self.assertEqual(sampler.study(0).get_trials(),[])

    def test_resume_pending_and_duplicate_tell(self):
        with tempfile.TemporaryDirectory() as directory:
            sampler=OptunaSampler(directory,{'x':list(range(12))},self.scope,seed=42,startup_trials=2)
            proposal=sampler.ask()
            restored=OptunaSampler(directory,{'x':list(range(12))},self.scope,seed=42,startup_trials=2)
            self.assertEqual(restored.pending()[0]['number'],proposal['number'])
            self.assertEqual(restored.tell(proposal['number'],self.observation()),'eligible')
            self.assertEqual(restored.tell(proposal['number'],self.observation()),'reused_receipt')
            self.assertNotEqual(restored.ask()['candidate_id'],proposal['candidate_id'])
            with self.assertRaises(ValueError):OptunaSampler(directory,{'x':list(range(12))},self.scope,seed=43,startup_trials=2)

    def test_power_failure_is_completed_with_real_objective_and_constraint(self):
        with tempfile.TemporaryDirectory() as directory:
            sampler=OptunaSampler(directory,{'x':[1,2,3]},self.scope)
            proposal=sampler.ask()
            self.assertEqual(sampler.tell(proposal['number'],self.observation(peak_power_w=21)),'power_failed')
            trial=sampler.study(0).get_trials()[0]
            self.assertEqual(trial.value,100)
            self.assertEqual(trial.user_attrs['constraints'],[1.0])

    def test_cache_and_scope_mismatch_never_create_fake_performance(self):
        with tempfile.TemporaryDirectory() as directory:
            sampler=OptunaSampler(directory,{'x':[1,2,3]},self.scope)
            proposal=sampler.ask()
            with self.assertRaises(ValueError):sampler.tell(proposal['number'],self.observation(hardware_hash='other'))
            self.assertEqual(sampler.tell(proposal['number'],self.observation(cache_reused=True)),'no_performance')
            self.assertIsNone(sampler.study(0).get_trials()[0].value)

    def test_finite_space_exhausts_without_repeat(self):
        with tempfile.TemporaryDirectory() as directory:
            sampler=OptunaSampler(directory,{'x':[1,2]},self.scope,algorithm='random')
            first=sampler.ask();second=sampler.ask()
            self.assertNotEqual(first['candidate_id'],second['candidate_id'])
            self.assertIsNone(sampler.ask())

    def test_completed_history_drives_tpe_and_duplicate_evidence_is_not_training(self):
        with tempfile.TemporaryDirectory() as directory:
            sampler=OptunaSampler(directory,{'x':list(range(20))},self.scope,seed=9,startup_trials=2)
            for index in range(2):
                proposal=sampler.ask()
                sampler.tell(proposal['number'],self.observation(id=f'fact{index}',cycles=100+proposal['params']['x']))
            third=sampler.ask()
            self.assertEqual(sampler.tell(third['number'],self.observation(id='fact0')),'no_performance')
            study=sampler.study(0)
            completed=[trial for trial in study.get_trials() if trial.state==sampler.optuna.trial.TrialState.COMPLETE]
            self.assertEqual(len(completed),2)

    def test_duplicate_suggestions_use_explicit_unseen_fallback(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            sampler=OptunaSampler(directory,{'x':[1,2]},self.scope)
            with patch.object(sampler.optuna.samplers.TPESampler,'sample_independent',return_value=1):
                first=sampler.ask();second=sampler.ask()
            self.assertNotEqual(first['candidate_id'],second['candidate_id'])
            self.assertEqual(second['proposal_origin'],'enumeration_fallback')
            self.assertIsNone(sampler.ask())

    def test_request_recovers_created_trial_before_answer_was_saved(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            sampler=OptunaSampler(directory,{'x':[1,2,3]},self.scope)
            with patch.object(sampler,'finish_ask',side_effect=RuntimeError('模拟回答前退出')):
                with self.assertRaises(RuntimeError):sampler.ask('request-1')
            self.assertEqual(len(sampler.pending()),1)
            restored=OptunaSampler(directory,{'x':[1,2,3]},self.scope)
            proposal=restored.ask('request-1')
            self.assertEqual(len(restored.pending()),1)
            self.assertEqual(restored.ask('request-1'),proposal)

    def test_recovery_after_duplicate_suggestion_does_not_return_existing_point(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            sampler=OptunaSampler(directory,{'x':[1,2]},self.scope)
            with patch.object(sampler.optuna.samplers.TPESampler,'sample_independent',return_value=1):
                first=sampler.ask('first')
                study=sampler.study(0)
                study.set_user_attr('pending_ask',{'request_id':'second','first_number':len(study.get_trials())})
                study.ask({'x':sampler.optuna.distributions.CategoricalDistribution([1,2])})
                recovered=sampler.ask('second')
            self.assertNotEqual(first['candidate_id'],recovered['candidate_id'])
            self.assertEqual(recovered['params']['x'],2)
