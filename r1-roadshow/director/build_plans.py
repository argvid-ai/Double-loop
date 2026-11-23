#!/usr/bin/env python3
"""Build model-checked camera plans without opening a motor device."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from trajectory import Planner
from plan import DEFAULT_MODEL, FPS, export
from logic import RULES
from standby import approved_start, check_start_frames, joint_vector


class DirectorPlanner(Planner):
    @staticmethod
    def smooth(u):
        # Zero velocity, acceleration, and jerk at either end of each move.
        return u**4 * (35 + u * (-84 + u * (70 - 20*u)))


def joint_recipe(planner, name, motion):
    half = np.deg2rad(motion['joint_span_deg']) / 2
    home = planner.start.copy()
    endpoints = []
    if name == 'JointAmplitudeCheck':
        for joint in range(7):
            for direction in (-1, 1, 0):
                q = home.copy()
                q[joint] += direction * half[joint]
                endpoints.append((f'J{joint+1}_{direction:+d}', q))
    elif name in ('ConversationSweep', 'GroupWide', 'FollowLeft', 'FollowRight'):
        # Model J1 rotates about -Z: negative is camera-left, positive camera-right.
        positions = {'ConversationSweep': [-1, 1, 0], 'GroupWide': [0, -1, 1, 0],
                     'FollowLeft': [-1, 0], 'FollowRight': [1, 0]}[name]
        for index, direction in enumerate(positions):
            q = home.copy()
            q[0] += direction * half[0]
            endpoints.append((f'{name}_{index}', q))
    elif name == 'RiseFollow':
        # Use optical-space IK instead of assuming raw encoder directions.
        position, rotation = planner.optical_pose(home)
        from trajectory import Waypoint
        from scipy.spatial.transform import Rotation
        raised = position + np.array([0., 0., .10])
        tilt = rotation * Rotation.from_euler('y', -10, degrees=True)
        rows = [home]
        for waypoint in (Waypoint('rise_headroom', raised, tilt, 8, motion['hold_s']),
                         Waypoint('rise_recenter', position, rotation, 8, motion['hold_s'])):
            states, seconds = planner.camera_move(rows[-1], waypoint)
            planner.add_section(rows, waypoint.name, states, seconds, waypoint.hold)
        return finish(planner, rows, name)
    else:
        raise ValueError(name)
    rows = [home]
    for label, endpoint in endpoints:
        endpoint = planner.joints(endpoint, label)
        states, seconds = planner.taught_move(rows[-1], endpoint, label, 4)
        planner.add_section(rows, label, states, seconds, motion['hold_s'])
    return finish(planner, rows, name)


def finish(planner, rows, name):
    motion_s = (len(rows) - 1) / 100
    recovery, kind = planner.return_path(rows)
    rows.extend(recovery[1:])
    return np.asarray(rows), dict(preset=name, mode=planner.profile['mode'], rate_hz=100, motion_s=motion_s,
        return_s=(len(recovery)-1)/100, return_path=kind, waypoints=planner.section_times,
        max_position_error_mm=planner.position_error*1000,
        max_orientation_error_deg=planner.orientation_error)


def build(config_path, model_path, output, field_profile=None, presets=None):
    config = json.loads(config_path.read_text())
    if config['mode'] != 'dry-run':
        raise ValueError('Expected the camera director configuration')
    motion = config['motion']
    profile_path = field_profile or config_path.parent / motion['profile']
    profile = json.loads(profile_path.read_text())
    mode = 'field' if field_profile else 'simulation'
    if profile['mode'] != mode:
        raise ValueError(f'Expected a {mode} motion profile')
    calibration_hash = None
    if field_profile:
        calibration_path = (profile_path.parent / profile['motor_calibration']).resolve()
        profile['motor_calibration'] = str(calibration_path)
        calibration_hash = hashlib.sha256(calibration_path.read_bytes()).hexdigest()
        for key in ('limit_rad_s', 'limit_rad_s2'):
            profile[key] = [min(a, b) for a, b in zip(joint_vector(profile[key]), joint_vector(motion[key]), strict=True)]
    else:
        profile.update(limit_rad_s=motion['limit_rad_s'], limit_rad_s2=motion['limit_rad_s2'])
    profile['travel_scale'] = motion['travel_scale']
    required_start = approved_start()
    model_start = joint_vector(profile['start_rad'])
    stable_s = required_start['stable_s']
    spans = np.asarray(motion['joint_span_deg'], dtype=float)
    if spans.shape != (7,) or not np.isfinite(spans).all() or np.any(spans <= 0):
        raise ValueError('joint_span_deg requires seven positive finite values')
    output.mkdir(parents=True, exist_ok=True)
    (output / 'profile.json').write_text(json.dumps(profile, indent=2) + '\n')
    manifest = {'mode': mode,
                'config_sha256': hashlib.sha256(config_path.read_bytes()).hexdigest(),
                'required_start': required_start, 'plans': {}}
    names = {action for action, _ in RULES.values()} - {'Hold'}
    if field_profile:
        manifest.update(model_start_rad=model_start, calibration_sha256=calibration_hash)
    else:
        manifest.update(simulation_start_rad=model_start, serial_enabled=False)
        names.update(('RoadshowIdleScan', 'JointAmplitudeCheck'))
    if presets:
        if not set(presets) <= names:
            raise ValueError(f'Unknown {mode} presets: {set(presets) - names}')
        names = set(presets)
    for name in sorted(names):
        planner = DirectorPlanner(model_path, profile)
        if name in ('ConversationSweep', 'GroupWide', 'FollowLeft', 'FollowRight', 'RiseFollow', 'JointAmplitudeCheck'):
            rows, report = joint_recipe(planner, name, motion)
        else:
            rows, report = planner.make(name, None)
        rows = np.vstack((np.repeat(rows[:1], stable_s * FPS, axis=0), rows))
        report['motion_s'] += stable_s
        for waypoint in report['waypoints']:
            waypoint['t_start_s'] += stable_s
        steps = np.diff(rows, axis=0)
        speed = steps * FPS
        accel = np.diff(np.vstack((np.zeros((1, 7)), speed, np.zeros((1, 7)))), axis=0)*FPS
        report.update(required_start=required_start, model_start_rad=model_start,
                      initial_hold_s=stable_s, easing='seventh_order',
                      joint_span_deg=np.rad2deg(np.ptp(rows, axis=0)).tolist(),
                      max_step_deg=np.rad2deg(np.abs(steps).max(axis=0)).tolist(),
                      peak_speed_rad_s=np.abs(speed).max(axis=0).tolist(),
                      peak_acceleration_rad_s2=np.abs(accel).max(axis=0).tolist())
        if np.any(np.abs(speed) > planner.vel + 1e-6) or np.any(np.abs(accel) > planner.acc + 4e-5):
            raise ValueError(f'{name}: failed full-plan derivative verification')
        path = output / f'{name}.r1traj'
        export(path, rows, planner, profile, output / 'profile.json', report)
        data = path.read_bytes()
        check_start_frames(data, name, model_start, stable_s, calibration_hash)
        (output / f'{name}.json').write_text(json.dumps(report, indent=2, ensure_ascii=False)+'\n')
        manifest['plans'][name] = {'file': path.name, 'sha256': hashlib.sha256(data).hexdigest(),
                                 'duration_s': report['motion_s']+report['return_s'],
                                 'joint_span_deg': report['joint_span_deg']}
        print(f'{name}: {manifest["plans"][name]["duration_s"]:.2f}s, spans={np.round(report["joint_span_deg"],1)}', flush=True)
    (output / 'manifest.json').write_text(json.dumps(manifest, indent=2)+'\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=HERE/'config.json')
    parser.add_argument('--model', type=Path, default=DEFAULT_MODEL)
    parser.add_argument('--out', type=Path)
    parser.add_argument('--field-profile', type=Path, help='Measured field profile and its calibration table')
    parser.add_argument('--preset', action='append', help='Build only this preset; repeat to build several')
    args = parser.parse_args()
    if args.field_profile and (not args.out or args.out.resolve() == (HERE/'plans').resolve()):
        parser.error('--field-profile needs its own --out directory')
    build(args.config.resolve(), args.model, args.out or HERE/'plans', args.field_profile, args.preset)
