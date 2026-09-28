"""同一个全局会话接收各方向的独立观测和完成事件。"""
import unittest

from codesign_lab.search.triggers import Triggers


class GlobalAnalysisModeTests(unittest.TestCase):
    def test_cross_lane_observations_and_completions_share_request(self):
        state={'source_epoch':'epoch','campaign':'campaign'}
        triggers=Triggers(state,global_only=True,batch_size=2,cooldown=0)
        triggers.observe('p1',{'id':'p1-case','case':'M1_P1','cycles':100,
            'peak_power_w':10,'functional_passed':True,'hardware_hash':'hw'})
        triggers.observe('d1',{'id':'d1-case','case':'M2_D1','cycles':50,
            'peak_power_w':10,'functional_passed':True,'hardware_hash':'hw'})
        targets={'p1-target':{'definition':{'lane':'p1'},'status':'DONE','candidates':{}},
            'd1-target':{'definition':{'lane':'d1'},'status':'ACTIVE','candidates':{}}}
        requests=triggers.poll(targets,1,now=100)
        self.assertEqual(len(requests),1)
        request=requests[0]
        self.assertEqual(request['lane'],'global')
        self.assertEqual(set(request['observation_ids']),{'p1-case','d1-case'})
        self.assertEqual(request['target_ids'],['p1-target'])
        self.assertIn('target_completion',request['reasons'])
        triggers.acknowledge('global',request['decision_id'],now=100)
        self.assertEqual(triggers.poll(targets,1,now=101),[])
        self.assertEqual(set(state['analysis']['lanes']),{'global'})

    def test_cached_observation_and_profile_route(self):
        triggers=Triggers({},global_only=True,batch_size=1,cooldown=0)
        triggers.observe('d1',{'id':'cached','cache_reused':True})
        triggers.profile_ready('p1','profile',{'status':'completed'})
        self.assertEqual(triggers.lane('global')['observations'],[])
        self.assertEqual(triggers.poll({},0,now=100)[0]['reasons'],['profile_ready'])


if __name__ == '__main__':
    unittest.main()
