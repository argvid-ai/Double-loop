#!/usr/bin/env python3
"""Check combined level-camera motions using the recorded single-joint probes."""
import argparse
import json
from pathlib import Path

import numpy as np
from scipy.spatial.transform import Rotation

from build_plans import DirectorPlanner, DEFAULT_MODEL, FPS, export
from standby import approved_start, check_start_frames


NOMINAL_PROFILE = Path(__file__).resolve().parent.parent / 'example.json'
LEVEL_AXES = [2, 3, 6]
ROUTES = {
    'ConversationSweep': (('left', 1.0), ('right', 1.0)),
    'GroupWide': (('left', 0.75), ('center', 1.5), ('right', 0.75)),
    'FollowLeft': (('left', 2.0),),
    'FollowRight': (('right', 2.0),),
}


def fit_optics(document, start):
    valid = [row for row in document['rows'] if row.get('inliers', 0) >= 40
             and row['median_reprojection_px'] < 1.0]
    recent = max(row['run'] for row in valid)
    samples = [row for row in valid if row['run'] == recent and row['joint'] in (1, 3, 4, 7)]
    offsets = np.rad2deg(np.asarray([row['q_sdk_rad'] for row in samples]) - start)
    image_angles = np.asarray([[row['pitch_deg'], row['roll_deg']] for row in samples])
    design = np.column_stack((np.ones(len(samples)), offsets[:, LEVEL_AXES]))
    intercept_and_slopes, _, rank, _ = np.linalg.lstsq(design, image_angles, rcond=None)
    residual = np.abs(design @ intercept_and_slopes - image_angles)
    if rank != len(LEVEL_AXES) + 1 or np.max(residual) > .35:
        raise ValueError('The most recent recording cannot resolve pitch and roll for J3, J4, and J7')
    return valid, intercept_and_slopes, np.max(residual, axis=0)


def level_offset(j4_deg, fit, limits):
    # J4 sets the amount of leaning back. Use J7 for pitch and J3 for roll.
    offset = np.zeros(7)
    offset[3] = j4_deg
    offset[[2, 6]] = np.linalg.solve(fit[[1, 3]].T, -fit[0] - j4_deg * fit[2])
    chosen = offset[LEVEL_AXES]
    if np.any(chosen < limits[0] - 1e-6) or np.any(chosen > limits[1] + 1e-6):
        raise ValueError('Level camera solution exceeds the observed single-joint range')
    return offset


def model_profile(geometry, sdk_start):
    profile = json.loads(NOMINAL_PROFILE.read_text())
    if profile['mode'] != 'simulation':
        raise ValueError('The local camera estimate is for rehearsal only')
    profile['start_rad'] = (np.asarray(geometry['local_model_anchor_rad']) + sdk_start
                            - geometry['sdk_reference_rad']).tolist()
    camera = np.asarray(geometry['candidate_link7_to_cv_camera_matrix'])
    optical_axes = camera[:, [2, 0, 1]] * [1, -1, -1]  # forward, left, up
    profile['camera'].update(
        link7_xyz_m=geometry['candidate_link7_to_lens_m'],
        link7_rpy_deg=Rotation.from_matrix(optical_axes).as_euler('xyz', degrees=True).tolist(),
    )
    profile.update(limit_rad_s=[.036, .024, .024, .024, .024, .024, .024],
                   limit_rad_s2=[.12, .10, .12, .10, .12, .10, .12])
    return profile


def pitch_roll(rotation):
    up = rotation.inv().apply([0., 0., 1.])
    return np.rad2deg([np.arcsin(np.clip(up[0], -1, 1)), np.arctan2(-up[1], up[2])])


def build_route(name, planner, profile_path, required, level, back, yaw, fit, out):
    hold_seconds = required['stable_s']
    home = planner.start
    rows = [home] * (hold_seconds * FPS + 1)
    level_q = home + np.deg2rad(level)
    back_q = home + np.deg2rad(back)

    def add(label, pose, seconds, hold):
        points, actual_seconds = planner.taught_move(rows[-1], pose, label, seconds)
        planner.add_section(rows, label, points, actual_seconds, hold)

    add('level_camera', level_q, 5, 1)
    level_start = len(rows) - 1
    add('lean_back_level', back_q, 4, 1)
    for side, hold in ROUTES[name]:
        pose = back_q.copy()
        pose[0] += np.deg2rad(yaw[side])
        add('frame_' + side, pose, 4, hold)
    add('center_while_level', level_q, 5, .75)
    end_level = len(rows)
    motion_seconds = (len(rows) - 1) / FPS
    add('return_to_21_06', home, 5, 0)
    rows = np.asarray(rows)
    offsets = np.rad2deg(rows - home)
    speed = np.diff(rows, axis=0) * FPS
    acceleration = np.diff(np.vstack((np.zeros((1, 7)), speed, np.zeros((1, 7)))), axis=0) * FPS
    if np.any(np.abs(speed) > planner.vel + 1e-6) or np.any(np.abs(acceleration) > planner.acc + 4e-5):
        raise ValueError(f'{name}: speed or acceleration failed at a section boundary')

    _, home_rotation = planner.optical_pose(home)
    model_bias = fit[0] - pitch_roll(home_rotation)
    level_slice = slice(level_start, end_level)
    estimated = fit[0] + offsets[level_slice, LEVEL_AXES] @ fit[1:]
    geometry_angles = np.asarray([pitch_roll(planner.optical_pose(q)[1]) for q in rows[level_slice]]) + model_bias
    level_position, level_rotation = planner.optical_pose(level_q)
    back_position, _ = planner.optical_pose(back_q)
    report = {
        'preset': name, 'mode': 'simulation', 'required_start': required,
        'model_start_rad': home.tolist(), 'rate_hz': FPS, 'motion_s': motion_seconds,
        'return_s': (len(rows) - 1) / FPS - motion_seconds, 'easing': 'seventh_order',
        'level_camera_offset_deg': level.tolist(), 'lean_back_offset_deg': back.tolist(),
        'model_backwards_lens_shift_mm': float((level_position-back_position) @ level_rotation.apply([1, 0, 0]) * 1000),
        'level_window_s': [level_start / FPS, (end_level - 1) / FPS],
        'max_local_fit_pitch_roll_deg': np.abs(estimated).max(axis=0).tolist(),
        'max_bias_adjusted_model_pitch_roll_deg': np.abs(geometry_angles).max(axis=0).tolist(),
        'joint_min_offset_deg': offsets.min(axis=0).tolist(),
        'joint_max_offset_deg': offsets.max(axis=0).tolist(),
        'max_frame_step_deg': np.rad2deg(np.abs(speed).max(axis=0) / FPS).tolist(),
        'waypoints': planner.section_times,
    }
    path = out / f'{name}.r1traj'
    export(path, rows, planner, planner.profile, profile_path, report)
    check_start_frames(path.read_bytes(), name, home, hold_seconds)
    (out / f'{name}.json').write_text(json.dumps(report, indent=2, allow_nan=False) + '\n')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--optics', required=True, type=Path)
    parser.add_argument('--geometry', required=True, type=Path)
    parser.add_argument('--model', default=DEFAULT_MODEL, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args()

    required = approved_start()
    start = np.asarray(required['q_sdk_rad'])
    valid, fit, residual = fit_optics(json.loads(args.optics.read_text()), start)
    observed = np.rad2deg(np.asarray([row['q_sdk_rad'] for row in valid]) - start)
    # Include the encoded standby as an endpoint even when the live readings differ slightly.
    limits = np.vstack((np.minimum(observed.min(axis=0), 0), np.maximum(observed.max(axis=0), 0)))
    level = level_offset(0., fit, limits[:, LEVEL_AXES])
    back = level_offset(limits[1, 3], fit, limits[:, LEVEL_AXES])
    yaw = {'left': float(limits[0, 0]), 'center': 0., 'right': float(limits[1, 0])}
    profile = model_profile(json.loads(args.geometry.read_text()), start)
    args.out.mkdir(parents=True, exist_ok=True)
    profile_path = args.out / 'profile.json'
    profile_path.write_text(json.dumps(profile, indent=2, allow_nan=False) + '\n')
    for name in ROUTES:
        planner = DirectorPlanner(args.model, profile)
        report = build_route(name, planner, profile_path, required, level, back, yaw, fit, args.out)
        print(name, 'max model pitch/roll', np.round(report['max_bias_adjusted_model_pitch_roll_deg'], 3), flush=True)
    # Export only the checked landmarks. The field controller plans slow moves from live feedback.
    vectors = (start, np.deg2rad(level), np.deg2rad(back), np.deg2rad([yaw['left'], yaw['right']]))
    (args.out / 'level-rehearsal.txt').write_text(
        'R1LEVEL1 ' + required['calibration_sha256'] + '\n'
        + ''.join(' '.join(f'{angle:.9f}' for angle in vector) + '\n' for vector in vectors))
    (args.out / 'optical-fit.json').write_text(json.dumps({
        'measurements': args.optics.name, 'sdk_start_rad': start.tolist(), 'fit_joints': ['J3', 'J4', 'J7'],
        'pitch_roll_intercept_and_slopes': fit.tolist(), 'max_sample_residual_deg': residual.tolist(),
        'observed_joint_bounds_deg': limits.tolist(),
    }, indent=2, allow_nan=False) + '\n')


if __name__ == '__main__':
    main()
