"""验证锁争用恢复、全局补目标和无变化时不重复消耗模型预算。"""
import copy
import errno
import fcntl
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from codesign_lab.search.decisions import apply_decision
from codesign_lab.search.locks import acquire
from codesign_lab.search.targets import TargetPool
from codesign_lab.search.triggers import Triggers


class SearchExhaustionChecks(unittest.TestCase):
    def test_real_lock_contention_waits_then_succeeds(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'lock'
            done=threading.Event()
            errors=[]
            def waiter():
                try:
                    with path.open('a') as handle:
                        acquire(handle,timeout=2,interval=.01)
                    done.set()
                except Exception as exc:
                    errors.append(exc)
            with path.open('a') as owner:
                fcntl.flock(owner,fcntl.LOCK_EX | fcntl.LOCK_NB)
                thread=threading.Thread(target=waiter)
                thread.start()
                time.sleep(.05)
                self.assertFalse(done.is_set())
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors,[])
            self.assertTrue(done.is_set())

    def test_other_lock_errors_propagate(self):
        with tempfile.TemporaryDirectory() as directory:
            with (Path(directory)/'lock').open('a') as handle:
                with patch('codesign_lab.search.locks.fcntl.flock',side_effect=OSError(errno.EIO,'I/O')):
                    with self.assertRaises(OSError):acquire(handle,timeout=.1)

    def test_global_review_is_once_per_terminal_revision(self):
        state={'source_epoch':'epoch','campaign':'test'}
        targets={'first':{'definition':{'lane':'p1_attention'},'status':'DONE','candidates':{}}}
        trigger=Triggers(state,cooldown=0)
        self.assertEqual(trigger.poll(targets,1,now=100,review_exhaustion=True)[0]['lane'],'p1_attention')
        requests=trigger.poll(targets,0,now=101,review_exhaustion=True)
        global_request=next(row for row in requests if row['lane']=='global')
        self.assertIn('pool_exhausted',global_request['reasons'])
        trigger.acknowledge('global',global_request['decision_id'],now=102)
        self.assertFalse(any(row['lane']=='global' for row in trigger.poll(targets,0,now=103,review_exhaustion=True)))
        targets['second']={'definition':{'lane':'hardware'},'status':'ACTIVE'}
        self.assertFalse(any(row['lane']=='global' for row in trigger.poll(targets,0,now=104,review_exhaustion=True)))
        targets['second']['status']='DONE'
        next_request=next(row for row in trigger.poll(targets,0,now=105,review_exhaustion=True) if row['lane']=='global')
        self.assertNotEqual(next_request['decision_id'],global_request['decision_id'])

    def test_global_decision_can_open_another_lane_atomically(self):
        records=[{'id':'base','config':{'hardware':{},'programs':{}}}]
        with tempfile.TemporaryDirectory() as directory:
            pool=TargetPool(directory,'epoch',max_proposals=3)
            pool.state['targets']['old']={'definition':{'lane':'p1_attention','max_trials':1},
                'status':'DONE','candidates':{}}
            trigger=Triggers(pool.state,cooldown=0)
            request=next(row for row in trigger.poll(pool.state['targets'],0,review_exhaustion=True)
                         if row['lane']=='global')
            target={'schema_version':1,'target_id':'next-hardware','lane':'hardware',
                'hypothesis':'交叉检查硬件与程序','base_record':'base','source_epoch':'epoch',
                'cases':['M1_P1','M2_D1'],'variables':[{'path':'hardware.reduction_units','values':[1,2]}],
                'sampler':'enumerate','max_trials':2,'priority':.7,'evidence_ids':['base']}
            decision={'schema_version':1,'decision_id':request['decision_id'],'lane':'global',
                'summary':'扩展到硬件方向','conclusions':[],'new_targets':[target],
                'stop_targets':[],'profile_requests':[],'implementation_proposals':[]}
            with patch('codesign_lab.search.decisions.read',return_value=records),\
                 patch('codesign_lab.search.targets.read',return_value=records),\
                 patch('codesign_lab.search.decisions.append_decision'):
                result=apply_decision(pool,trigger,decision)
            self.assertEqual(result['targets_added'],1)
            self.assertEqual(pool.state['targets']['next-hardware']['status'],'QUEUED')
            self.assertIsNone(trigger.lane('global')['pending'])
            self.assertTrue(trigger.lane('global')['reviewed_pool_revision'])
            bad=copy.deepcopy(decision)
            bad['decision_id']='a'*64
            bad['new_targets'][0]['lane']='global'
            trigger.lane('global')['pending']={'decision_id':'a'*64,'observation_ids':[],
                'target_ids':[],'target_revisions':{}}
            with patch('codesign_lab.search.decisions.read',return_value=records):
                with self.assertRaises(ValueError):apply_decision(pool,trigger,bad)
