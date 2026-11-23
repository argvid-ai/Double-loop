#!/usr/bin/env python3
"""Inspect snapshots or fit an offline candidate motor-to-model calibration."""
import argparse
import json
import math
from pathlib import Path

# Right-arm SDK URDF limits, preserving its J6 +/-45-degree model interval.
# Actual usable travel is recorded separately in the field profile.
BOUNDS_DEG = ((-156, 156), (0, 180), (-20, 200), (0, 180), (-130, 130), (-45, 45), (-90, 90))


def vector(values, name):
    if (not isinstance(values, list) or len(values) != 7 or
            any(not isinstance(x, (int, float)) or isinstance(x, bool) or not math.isfinite(x) for x in values)):
        raise ValueError(f'{name} needs seven finite numbers')
    return values


def read_snapshot(path):
    rows = [json.loads(line[len('SNAPSHOT '):]) for line in path.read_text().splitlines()
            if line.startswith('SNAPSHOT ')]
    if not rows:
        raise ValueError('no SNAPSHOT line in the supplied log')
    snapshot = rows[-1]
    if snapshot.get('mode') != 0:
        raise ValueError('expected a stopped snapshot')
    vector(snapshot['q_sdk_rad'], 'q_sdk_rad')
    vector(snapshot['q_motor_rad'], 'q_motor_rad')
    return snapshot


def inspect(path):
    snapshot = read_snapshot(path)
    axes = []
    for j, value in enumerate(snapshot['q_sdk_rad']):
        low, high = BOUNDS_DEG[j]
        degrees = math.degrees(value)
        equivalent = math.degrees(math.remainder(value, math.tau))
        axes.append({
            'joint': j + 1, 'sdk_deg': degrees, 'motor_rad': snapshot['q_motor_rad'][j],
            'within_nominal_model_interval': low <= degrees <= high,
            'whole_turn_equivalent_deg': equivalent,
            'whole_turn_equivalent_within_interval': low <= equivalent <= high,
            'nominal_model_interval_deg': [low, high],
        })
    return {'axes': axes, 'note': 'Equivalent angles alone do not establish the physical model zero or direction.'}


def template(path):
    snapshot = read_snapshot(path)
    return {'joints': [
        {'joint': j + 1, 'observations': [
            {'motor_rad': motor, 'model_deg': None, 'label': 'measured reference A'},
            {'motor_rad': None, 'model_deg': None, 'label': 'measured reference B'},
            {'motor_rad': None, 'model_deg': None, 'label': 'independent verification C'},
        ]}
        for j, motor in enumerate(snapshot['q_motor_rad'])
    ]}


def fit(payload):
    joints = payload['joints']
    if not isinstance(joints, list) or len(joints) != 7:
        raise ValueError('provide observations for all seven joints')
    rows = {}
    diagnostics = []
    for joint in joints:
        number = joint['joint']
        if type(number) is not int or number not in range(1, 8) or number in rows:
            raise ValueError('joint must be unique, numbered 1 through 7')
        points = joint['observations']
        if len(points) < 2:
            raise ValueError(f'J{number}: two known poses are needed to determine scale and zero')
        known = []
        for point in points:
            pair = point['motor_rad'], point['model_deg']
            if (any(not isinstance(x, (int, float)) or isinstance(x, bool) or not math.isfinite(x) for x in pair)):
                raise ValueError(f'J{number}: motor_rad and model_deg must contain measured numbers')
            known.append((pair[0], math.radians(pair[1])))
        (motor_a, angle_a), (motor_b, angle_b) = known[:2]
        if abs(motor_b - motor_a) < 1e-5:
            raise ValueError(f'J{number}: reference motor readings are indistinguishable')
        slope = (angle_b - angle_a) / (motor_b - motor_a)
        if not 0 < abs(slope) <= 10:
            raise ValueError(f'J{number}: inferred scale is outside the playback calibration range')
        offset = angle_a - slope * motor_a
        residual = [math.degrees(angle - (slope * motor + offset)) for motor, angle in known[2:]]
        rows[number] = (math.copysign(1, slope), abs(slope), offset)
        diagnostics.append({
            'joint': number, 'dir': rows[number][0], 'scale': abs(slope), 'offset_rad': offset,
            'independent_residual_deg': residual,
            'has_independent_verification': bool(residual),
        })
    output = '# candidate calibration fitted from measured model-angle references\n# joint dir scale offset\n'
    for joint in range(1, 8):
        direction, scale, offset = rows[joint]
        output += f'{joint - 1} {direction:.0f} {scale:.12g} {offset:.12g}\n'
    return output, {'joints': sorted(diagnostics, key=lambda row: row['joint'])}


def save_new(path, text):
    with path.open('x') as file:
        file.write(text)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for command in ('inspect', 'template'):
        action = sub.add_parser(command)
        action.add_argument('snapshot', type=Path)
        action.add_argument('--out', type=Path)
    action = sub.add_parser('fit')
    action.add_argument('observations', type=Path)
    action.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == 'fit':
            data, report = fit(json.loads(args.observations.read_text()))
            save_new(args.out, data)
            print(json.dumps(report, indent=2, allow_nan=False))
        else:
            data = inspect(args.snapshot) if args.command == 'inspect' else template(args.snapshot)
            text = json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + '\n'
            if args.out:
                save_new(args.out, text)
            print(text, end='')
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(1, f'Calibration preparation failed: {error}\n')


if __name__ == '__main__':
    main()
