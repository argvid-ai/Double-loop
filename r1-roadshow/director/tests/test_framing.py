import sys
from pathlib import Path
from types import SimpleNamespace
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from framing import assess_framing


def subject(x, y, bottom=98, hide_hips=False):
    box = (x - 12, y, x + 12, bottom)
    points = np.zeros((17, 3))
    points[[0, 5, 6, 11, 12]] = [[x, y+7, .9], [x-7, y+17, .9], [x+7, y+17, .9],
                                  [x-5, y+43, .9], [x+5, y+43, .9]]
    if hide_hips:
        points[11:13, 2] = 0
    return {'xyxy': box}, SimpleNamespace(box=box, points=points, confidence=.9)


class FramingTests(unittest.TestCase):
    def check(self, *subjects):
        detections, poses = zip(*subjects)
        return assess_framing(detections, poses, (100, 160, 3))

    def test_one_high_head_blocks_whole_group_even_with_detected_face(self):
        report = self.check(subject(45, 0), subject(94, 12))
        self.assertEqual(report['status'], 'tilt_up')
        self.assertEqual([p['ready'] for p in report['people']], [False, True])

    def test_legs_may_extend_outside_frame(self):
        self.assertEqual(self.check(subject(45, 10), subject(94, 18))['status'], 'ready')

    def test_visible_head_with_small_clearance_fits(self):
        self.assertEqual(self.check(subject(45, 3))['status'], 'ready')

    def test_combined_upper_bodies_too_tight_to_fit_by_tilting(self):
        report = self.check(subject(45, 0), subject(94, 55))
        self.assertEqual(report['status'], 'blocked')

    def test_unverified_waist_is_not_ready(self):
        report = self.check(subject(60, 18, hide_hips=True))
        self.assertEqual(report['status'], 'tilt_down')

    def test_primary_detection_without_matching_pose_is_not_ready(self):
        report = assess_framing([subject(45, 12)[0]], [], (100, 160, 3))
        self.assertEqual(report['status'], 'blocked')

    def test_pose_only_without_torso_anchors_does_not_trigger(self):
        ghost = subject(45, 12, hide_hips=True)[1]
        self.assertEqual(assess_framing([], [ghost], (100, 160, 3))['status'], 'empty')
