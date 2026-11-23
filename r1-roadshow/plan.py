#!/usr/bin/env python3
"""Plan R1-7A camera moves using the existing right-arm MuJoCo model."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile

import presets

HERE = Path(__file__).resolve().parent
DEFAULT_MODEL = HERE.parent.parent / 'r1 7a机械臂/models/mjcf/right.xml'
FPS = 100


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('preset', nargs='?')
    parser.add_argument('--list', action='store_true')
    parser.add_argument('--profile', type=Path, default=HERE / 'example.json')
    parser.add_argument('--model', type=Path, default=DEFAULT_MODEL)
    parser.add_argument('--out', type=Path, help='Save the validated joint plan as .r1traj')
    parser.add_argument('--report', type=Path, help='Save camera poses and coverage checks as JSON')
    parser.add_argument('--narration-holds', type=float, nargs='+', help='One duration in seconds per narrated step')
    args = parser.parse_args()
    if args.list:
        for group, names in (
            ('Source code', presets.PORTRAIT),
            ('Adapted from design', presets.PORTRAIT_DESIGN),
            ('Auxiliary', presets.AUXILIARY),
            ('Needs live vision', presets.DESIGN_REQUIRES_VISION),
        ):
            for name in names:
                print(f'{name}\t{group}')
        return
    if args.preset in presets.DESIGN_REQUIRES_VISION:
        parser.error(f'{args.preset} requires {presets.DESIGN_REQUIRES_VISION[args.preset]}')
    if args.preset not in (*presets.TEMPLATES, *presets.AUXILIARY):
        parser.error('Choose a preset from --list')
    if args.narration_holds and args.preset != 'PortraitNarrativeShowcasePreset':
        parser.error('--narration-holds applies only to PortraitNarrativeShowcasePreset')

    try:
        from trajectory import Planner

        profile = json.loads(args.profile.read_text())
        planner = Planner(args.model, profile)
        rows, report = planner.make(args.preset, args.narration_holds)
        if args.out:
            export(args.out, rows, planner, profile, args.profile, report)
        payload = json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False)
        if args.report:
            args.report.write_text(payload + '\n')
        print(f'{args.preset}  {report["motion_s"]:.2f}s + {report["return_s"]:.2f}s return  '
              f'{len(rows)} samples @ {FPS} Hz  {report["max_position_error_mm"]:.2f} mm  '
              f'{report["max_orientation_error_deg"]:.2f}°  {profile["mode"]}')
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
        parser.exit(1, f'Planning failed: {error}\n')


def export(path, rows, planner, profile, profile_path, report):
    # Complete the validated plan before publishing any executable file.
    # Bind a field plan to the exact encoder table used to produce it.
    calibration = '-'
    if profile['mode'] == 'field':
        source = Path(profile['motor_calibration'])
        if not source.is_absolute():
            source = profile_path.resolve().parent / source
        calibration = hashlib.sha256(source.read_bytes()).hexdigest()
    with tempfile.NamedTemporaryFile(mode='w', dir=path.resolve().parent, delete=False) as handle:
        temp = Path(handle.name)
        try:
            shot_end = round(report["motion_s"] * FPS)
            handle.write(f'R1TRAJ1 {profile["mode"]} {FPS} {calibration} {len(rows)} {shot_end}\n')
            for vector in (planner.low, planner.high, planner.vel, planner.acc):
                handle.write(' '.join(f'{value:.9f}' for value in vector) + '\n')
            if profile['mode'] == 'field':
                for key, cap in (('kp', 100), ('kd', 10), ('torque_limit_nm', 15)):
                    vector = planner.checked_vector(profile[key], 7, key)
                    if min(vector) <= 0 or max(vector) > cap:
                        raise ValueError(f'{key} values must be in (0, {cap}]')
                    handle.write(' '.join(f'{value:.9f}' for value in vector) + '\n')
            for q in rows:
                handle.write(' '.join(f'{value:.9f}' for value in q) + '\n')
            handle.close()
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)


if __name__ == '__main__':
    main()
