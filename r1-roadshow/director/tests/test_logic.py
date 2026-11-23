import json
import math
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from logic import Director, RULES, observation


def sample(scene='two_conversation', people=2, confidence=.9):
    return dict(scene=scene, people=people, confidence=confidence, evidence='两人面对面，互相做手势')


class LogicTests(unittest.TestCase):
    def test_conversation_requires_two_fresh_observations(self):
        director = Director()
        self.assertEqual(director.decide(sample(), 10, 9)['status'], 'confirming')
        self.assertEqual(director.decide(sample(), 12, 11)['action'], 'ConversationSweep')

    def test_two_people_alone_are_not_conversation(self):
        director = Director(confirmations=1)
        self.assertEqual(director.decide(sample('two_independent'), 10, 9)['action'], 'GroupWide')

    def test_count_disagreement_blocks_sweep(self):
        self.assertEqual(Director(confirmations=1).decide(sample(),10,9,1)['action'],'Hold')
        self.assertEqual(Director(confirmations=1).decide(sample(people=3),10,9)['action'],'Hold')

    def test_invalid_output_and_nonfinite_values(self):
        for value in ('not json', '{}', '{"scene":"execute_shell"}', sample(confidence=math.nan),
                      sample(people=True), sample(confidence=True), sample(people=-1)):
            self.assertEqual(Director(confirmations=1).decide(value,10,9)['action'],'Hold')

    def test_single_static_image_cannot_trigger_motion_inference(self):
        for scene in ('walking_left','walking_right','sit_to_stand'):
            self.assertEqual(Director(confirmations=1).decide(sample(scene,1),10,9,temporal=False)['action'],'Hold')

    def test_unknown_occlusion_low_confidence_and_stale(self):
        for value, now, stamp in ((sample('uncertain'),10,9), (sample('occluded'),10,9),
                                  (sample(confidence=.6),10,9), (sample(),20,1),
                                  (sample(),10,11), (sample(),math.nan,9)):
            self.assertEqual(Director(confirmations=1).decide(value,now,stamp)['action'],'Hold')

    def test_duplicate_and_gap_do_not_accumulate_confirmation(self):
        director = Director()
        director.decide(sample(),10,9)
        self.assertEqual(director.decide(sample(),11,9)['action'],'Hold')
        self.assertEqual(director.decide(sample(),12,11)['status'],'confirming')
        self.assertEqual(director.decide(sample(),30,29)['status'],'confirming')

    def test_active_shot_is_not_restarted_each_inference(self):
        director = Director(confirmations=1)
        self.assertEqual(director.decide(sample(),10,9)['status'],'ready')
        director.started(10,60)
        self.assertEqual(director.decide(sample(),20,19)['status'],'busy')
        self.assertEqual(director.decide(sample(),71,70)['status'],'ready')

    def test_empty_stays_at_standby_until_a_scene_triggers(self):
        director = Director()
        for now in (10, 12, 14):
            idle = director.decide(sample('empty', 0), now, now - 1)
            self.assertEqual((idle['status'], idle['action'], idle['scene']), ('hold', 'Hold', 'empty'))
        self.assertEqual(director.decide(sample(), 16, 15)['status'], 'confirming')
        self.assertEqual(director.decide(sample(), 18, 17)['action'], 'ConversationSweep')

    def test_each_scene_mapping(self):
        for scene, (action, _) in RULES.items():
            people = 2 if scene.startswith('two_') else 3 if scene.startswith('group_') else 0 if scene=='empty' else 1
            result = Director(confirmations=1).decide(sample(scene,people),10,9)
            self.assertEqual(result['action'],action)

    def test_only_optional_json_fence_is_allowed(self):
        raw=json.dumps(sample())
        self.assertEqual(observation('```json\n'+raw+'\n```')['scene'],'two_conversation')
        for extra in ('hello '+raw, raw+'\n'+raw):
            with self.assertRaises(ValueError):
                observation(extra)


if __name__ == '__main__':
    unittest.main()
