"""Use YOLO pose landmarks to check every person's head, shoulders, and waist."""
from contextlib import redirect_stdout
import os
from pathlib import Path
import sys

import numpy as np


class PoseDetector:
    def __init__(self, model):
        model = Path(model).expanduser().resolve(strict=True)
        sys.path.insert(0, str(model.parent.parent))
        try:
            from pose_preview import EngineInfer, detect_pose
        finally:
            sys.path.pop(0)
        self.detect_pose = detect_pose
        self.model_path = model
        self.quiet = open(os.devnull, 'w')
        try:
            with redirect_stdout(self.quiet):
                self.engine = EngineInfer(str(model))
        except BaseException:
            self.quiet.close()
            raise

    def detect(self, frame):
        with redirect_stdout(self.quiet):
            people, latency = self.detect_pose(self.engine, frame)
        return people, round(latency, 1)

    def close(self):
        try:
            with redirect_stdout(self.quiet):
                self.engine.clean()
        finally:
            self.quiet.close()


def box_overlap(a, b):
    area_a = (a[2] - a[0]) * (a[3] - a[1])
    area_b = (b[2] - b[0]) * (b[3] - b[1])
    common = max(0., min(a[2], b[2]) - max(a[0], b[0])) * max(0., min(a[3], b[3]) - max(a[1], b[1]))
    return common / (area_a + area_b - common) if common else 0.


def confident_points(points):
    return np.isfinite(points).all(axis=1) & (points[:, 2] >= .45)


def has_torso_anchor(pose):
    valid = confident_points(pose.points)
    return valid[5:7].all() and valid[11:13].any()


def upper_body(person_box, pose, width, height):
    headroom = .022 * height
    footroom = .055 * height
    sides = .035 * width
    reasons = []
    # A detector box at the ceiling of the image can hide part of the head even
    # when YOLO can still confidently locate a nose or an ear.
    if min(person_box[1], pose.box[1] if pose else height) < headroom:
        reasons.append('head_high')
    if not pose:
        return reasons + ['pose_missing'], None
    points = pose.points
    valid = confident_points(points)
    head, shoulders, hips = points[:5][valid[:5]], points[5:7][valid[5:7]], points[11:13][valid[11:13]]
    if not len(head):
        reasons.append('head_unverified')
    if len(shoulders) != 2:
        reasons.append('shoulders_unverified')
    if not len(hips):
        reasons.append('waist_unverified')
        if person_box[3] >= height - footroom:
            reasons.append('waist_low')
    if len(hips) and max(hips[:, 1]) >= height - footroom:
        reasons.append('waist_low')
    torso = np.concatenate((head, shoulders, hips))
    if not len(torso):
        return reasons, None
    if min(torso[:, 0]) < sides:
        reasons.append('upper_body_left')
    if max(torso[:, 0]) >= width - sides:
        reasons.append('upper_body_right')
    bounds = [float(min(torso[:, 0]) / width), float(person_box[1] / height),
              float(max(torso[:, 0]) / width), float(max(torso[:, 1]) / height)]
    return reasons, bounds


def assess_framing(detections, poses, shape):
    height, width = shape[:2]
    boxes = [item['xyxy'] for item in detections]
    pairs = sorted(((box_overlap(box, pose.box), i, j)
                    for i, box in enumerate(boxes) for j, pose in enumerate(poses)), reverse=True)
    assigned = {}
    used = set()
    for overlap, i, j in pairs:
        if overlap < .28:
            break
        if i not in assigned and j not in used:
            assigned[i] = poses[j]
            used.add(j)
    # A strong pose may see a cropped person that the primary detector missed.
    inputs = [(box, assigned.get(i)) for i, box in enumerate(boxes)]
    inputs.extend((pose.box, pose) for j, pose in enumerate(poses) if j not in used and pose.confidence >= .55
                  and has_torso_anchor(pose)
                  and not any(box_overlap(box, pose.box) >= .28 for box in boxes))
    people = []
    for box, pose in sorted(inputs, key=lambda item: (item[0][0] + item[0][2]) / 2):
        reasons, bounds = upper_body(box, pose, width, height)
        people.append({'ready': not reasons, 'issues': reasons, 'upper_body_xyxy_norm': bounds})
    issues = {reason for person in people for reason in person['issues']}
    if not people:
        status = 'empty'
    elif not issues:
        status = 'ready'
    elif 'head_high' in issues and 'waist_low' not in issues:
        status = 'tilt_up'
    elif 'waist_low' in issues and 'head_high' not in issues:
        status = 'tilt_down'
    else:
        status = 'blocked'
    return {'status': status, 'people': people}
