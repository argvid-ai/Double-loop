from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

import numpy as np

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from vision import VLM, movement_candidates


class VisionTests(unittest.TestCase):
    def make(self, label, finish='stop'):
        vlm=VLM(dict(endpoint='http://example.invalid',model='test',request_timeout_s=7))
        vlm.request=Mock(return_value=({'finish_reason':finish,'message':{'content':label}}, {}))
        return vlm

    def test_exact_label_without_fabricated_confidence(self):
        vlm=self.make('双人交流')
        vlm.request.side_effect=[({'finish_reason':'stop','message':{'content':'双人交流'}},{}),
                                 ({'finish_reason':'stop','message':{'content':'是。'}},{})]
        audit,_=vlm.classify([np.zeros((100,100,3),dtype=np.uint8)],2)
        self.assertEqual(audit['observation']['scene'],'two_conversation')
        self.assertNotIn('confidence',audit['observation'])

    def test_two_people_facing_away_do_not_trigger_conversation(self):
        vlm=self.make('双人交流')
        vlm.request.side_effect=[({'finish_reason':'stop','message':{'content':'双人交流'}},{}),
                                 ({'finish_reason':'stop','message':{'content':'否'}},{})]
        audit,_=vlm.classify([np.zeros((100,100,3),dtype=np.uint8)],2)
        self.assertEqual(audit['observation']['scene'],'two_independent')

    def test_unoffered_label_and_truncation_fail(self):
        for label,count,finish in [('双人交流',3,'stop'),('执行代码',2,'stop'),
                                    ('多人走动',2,'stop'),('双人交流',2,'length')]:
            audit,_=self.make(label,finish).classify([np.zeros((100,100,3),dtype=np.uint8)],count)
            self.assertIn('error',audit)
            self.assertNotIn('observation',audit)

    def test_motion_candidates_require_one_person_and_matching_geometry(self):
        first=[dict(xyxy=[100,100,200,300])]
        self.assertEqual(movement_candidates(first,[dict(xyxy=[180,100,280,300])],(500,500),(500,500)),['向右走'])
        self.assertEqual(movement_candidates(first,[dict(xyxy=[100,50,200,350])],(500,500),(500,500)),['起立'])
        self.assertEqual(movement_candidates(first,first,(500,500),(500,500)),[])
        self.assertEqual(movement_candidates(first,first*2,(500,500),(500,500)),[])


if __name__ == '__main__':
    unittest.main()
