#!/usr/bin/env python3
"""Measure camera pitch and roll from clips using the recorded J1 sweep as reference."""
import argparse
import json
from pathlib import Path

import cv2
import numpy as np
from scipy.optimize import least_squares


SIZE = (1280, 720)
CENTER = (np.asarray(SIZE) - 1) / 2


def clip_frame(capture, seconds):
    capture.set(cv2.CAP_PROP_POS_MSEC, 1000 * seconds)
    ok, frame = capture.read()
    if not ok:
        raise ValueError(f'Could not read video at {seconds:.2f}s')
    return frame


class OpticalReference:
    def __init__(self, archive, session, telemetry, still, sweep):
        self.detect = cv2.SIFT_create(nfeatures=7000, contrastThreshold=.012)
        self.match = cv2.BFMatcher()
        first = json.loads(session.read_text())['first_video_observed_wall_s']
        rows = [json.loads(line.removeprefix('TELEMETRY ')) for line in telemetry.read_text().splitlines()
                if line.startswith('TELEMETRY ')]

        def yaw(t):
            return min(rows, key=lambda row: abs(row['wall_s'] - first - t))['q_rad'][0]

        source = cv2.VideoCapture(str(archive))
        try:
            self.reference = self.features(clip_frame(source, still))
            pairs = [(yaw(t) - yaw(still), *self.matches(self.features(clip_frame(source, t)))) for t in sweep]
        finally:
            source.release()
        if min(len(ids) for _, _, _, ids in pairs) < 45:
            raise ValueError('The reference sweep has too few matching landmarks')

        def relative_pose(parameters, delta):
            _, ax, az, gamma = parameters
            axis = np.array([ax, 1., az])
            axis /= np.linalg.norm(axis)
            z = np.array([0., 0., 1.]) - axis[2] * axis
            z /= np.linalg.norm(z)
            point = np.cos(gamma) * z + np.sin(gamma) * np.cross(axis, z)
            rotation = cv2.Rodrigues(-delta * axis)[0]
            return rotation, (np.eye(3) - rotation) @ point, axis

        def rays(points, focal):
            return np.c_[(points - CENTER) / focal, np.ones(len(points))]

        def errors(parameters):
            focal = parameters[0]
            residuals = []
            for delta, first_points, second_points, _ in pairs:
                rotation, translation, _ = relative_pose(parameters, delta)
                x, y, z = translation
                essential = np.array([[0., -z, y], [z, 0., -x], [-y, x, 0.]]) @ rotation
                first_rays, second_rays = rays(first_points, focal), rays(second_points, focal)
                e_first, e_second = first_rays @ essential.T, second_rays @ essential
                norm = np.sqrt(np.sum(e_first[:, :2]**2 + e_second[:, :2]**2, axis=1))
                residuals.extend(np.sum(e_first * second_rays, axis=1) * focal / norm)
            return residuals

        fit = least_squares(errors, [1005., 0., .088, .1], bounds=([800, -.14, -.2, -1.4], [1220, .14, .26, 1.4]),
                            loss='soft_l1', f_scale=.6)
        if not fit.success or np.median(np.abs(fit.fun)) > 1.0:
            raise ValueError('The reference sweep could not calibrate the image')
        focal = fit.x[0]
        self.intrinsic = np.array([[focal, 0., CENTER[0]], [0., focal, CENTER[1]], [0., 0., 1.]])
        delta, first_points, second_points, ids = pairs[1]
        rotation, translation, axis = relative_pose(fit.x, delta)
        cloud = cv2.triangulatePoints(np.c_[np.eye(3), np.zeros(3)], np.c_[rotation, translation],
                                     rays(first_points, focal)[:, :2].T, rays(second_points, focal)[:, :2].T)
        points = (cloud[:3] / cloud[3]).T
        sign = np.sign(np.median(points[:, 2]))
        points *= sign
        moved = points @ rotation.T + translation * sign

        def project(points):
            return points[:, :2] / points[:, 2, None] * focal + CENTER

        good = (points[:, 2] > 1) & (points[:, 2] < 120) & (moved[:, 2] > 1)
        good &= (np.linalg.norm(project(points)-first_points, axis=1) < 1.)
        good &= (np.linalg.norm(project(moved)-second_points, axis=1) < 1.)
        self.landmarks = dict(zip(ids[good], points[good]))
        self.up = -axis

    def features(self, frame):
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.resize(gray, SIZE, interpolation=cv2.INTER_AREA)
        return self.detect.detectAndCompute(gray, None)

    def matches(self, frame):
        first, d0 = self.reference
        second, d1 = frame
        if d0 is None or d1 is None or min(len(d0), len(d1)) < 2:
            return np.empty((0, 2)), np.empty((0, 2)), np.empty(0, dtype=int)
        reverse = {m.queryIdx: m.trainIdx for m, n in self.match.knnMatch(d1, d0, k=2) if m.distance < .74*n.distance}
        common = [m for m, n in self.match.knnMatch(d0, d1, k=2)
                  if m.distance < .74*n.distance and reverse.get(m.trainIdx) == m.queryIdx]
        return (np.asarray([first[m.queryIdx].pt for m in common]), np.asarray([second[m.trainIdx].pt for m in common]),
                np.asarray([m.queryIdx for m in common]))

    def measure(self, frame):
        _, points, ids = self.matches(self.features(frame))
        common = [i for i, key in enumerate(ids) if key in self.landmarks]
        if len(common) < 45:
            return {'landmarks': len(common), 'error': 'Insufficient reference landmarks'}
        world = np.asarray([self.landmarks[ids[i]] for i in common])
        observed = points[common]
        ok, rotation, offset, inliers = cv2.solvePnPRansac(world, observed, self.intrinsic, None, iterationsCount=500,
                                                          reprojectionError=2., confidence=.999, flags=cv2.SOLVEPNP_EPNP)
        if not ok or len(inliers) < 40:
            return {'landmarks': len(common), 'error': 'Could not verify camera pose'}
        use = inliers[:, 0]
        rotation, offset = cv2.solvePnPRefineLM(world[use], observed[use], self.intrinsic, None, rotation, offset)
        image = cv2.projectPoints(world[use], rotation, offset, self.intrinsic, None)[0][:, 0]
        up = cv2.Rodrigues(rotation)[0] @ self.up
        return {'pitch_deg': float(np.rad2deg(np.arcsin(np.clip(up[2], -1, 1)))),
                'roll_deg': float(np.rad2deg(np.arctan2(up[0], -up[1]))),
                'inliers': len(inliers), 'median_reprojection_px': float(np.median(np.linalg.norm(image-observed[use], axis=1)))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', required=True, type=Path)
    parser.add_argument('--session', required=True, type=Path)
    parser.add_argument('--telemetry', required=True, type=Path)
    parser.add_argument('--reference-at', required=True, type=float)
    parser.add_argument('--sweep-at', required=True, type=float, nargs=4)
    parser.add_argument('--clip', required=True, type=Path)
    parser.add_argument('--at', required=True, type=float, nargs='+')
    args = parser.parse_args()
    reference = OpticalReference(args.archive, args.session, args.telemetry, args.reference_at, args.sweep_at)
    clip = cv2.VideoCapture(str(args.clip))
    try:
        results = [{'clip_seconds': t, **reference.measure(clip_frame(clip, t))} for t in args.at]
    finally:
        clip.release()
    print(json.dumps({'clip': str(args.clip), 'vertical_reference': 'J1 rotation axis', 'samples': results},
                     indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
