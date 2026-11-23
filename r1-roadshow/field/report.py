#!/usr/bin/env python3
"""Summarize one recorded R1 playback log without accessing hardware."""
import argparse
import json
import math
from pathlib import Path


def summarize(path):
    lines = path.read_text(errors='replace').splitlines()
    frames = []
    rejected = []
    returned = any(line in ('Returned to start and verified mode 0 on all joints',
                            'Parked and verified mode 0 on all joints') for line in lines)
    failure = next((line for line in reversed(lines) if 'Replay failed:' in line), None)
    for line in lines:
        if line.startswith('TELEMETRY '):
            try:
                row = json.loads(line.removeprefix('TELEMETRY '))
                for key in ('target_rad', 'q_rad', 'dq_rad_s', 'tau_nm'):
                    values = row[key]
                    if len(values) != 7 or any(not isinstance(x, (int, float)) or not math.isfinite(x) for x in values):
                        raise ValueError('invalid joint vector')
                if not math.isfinite(row['wall_s']) or not math.isfinite(row['elapsed_s']):
                    raise ValueError('invalid timestamp')
                frames.append(row)
            except (ValueError, KeyError, TypeError) as error:
                rejected.append(str(error))
    result = {
        'started': any(line in ('PLAYBACK_START', 'AXIS_PROBE_START') or line.startswith(
            ('REHEARSAL_START ', 'LEVEL_RETREAT_VERIFY_REFERENCE ')) for line in lines),
        'returned_powered': any(line == 'AXIS_PROBE_COMPLETE' or line.startswith(
            ('REHEARSAL_RETURNED_POWERED ', 'LEVEL_RETREAT_RETURNED_POWERED ')) for line in lines)
            and not returned and failure is None,
        'returned_and_stopped': returned,
        'failure': failure,
        'telemetry_samples': len(frames),
        'invalid_telemetry_lines': rejected,
    }
    if frames:
        result.update({
            'wall_start_s': frames[0]['wall_s'],
            'wall_end_s': frames[-1]['wall_s'],
            'observed_s': frames[-1]['elapsed_s'] - frames[0]['elapsed_s'],
            'j1_range_deg': math.degrees(max(row['q_rad'][0] for row in frames) -
                                        min(row['q_rad'][0] for row in frames)),
            'joint_peak_tracking_error_deg': [
                math.degrees(max(abs(row['q_rad'][j] - row['target_rad'][j]) for row in frames))
                for j in range(7)],
            'joint_peak_torque_nm': [max(abs(row['tau_nm'][j]) for row in frames) for j in range(7)],
            'joint_peak_speed_rad_s': [max(abs(row['dq_rad_s'][j]) for row in frames) for j in range(7)],
            'j1_target_extremes': [
                {'target_rad': row['target_rad'][0], 'actual_rad': row['q_rad'][0], 'wall_s': row['wall_s']}
                for row in (
                    min(frames, key=lambda value: value['target_rad'][0]),
                    max(frames, key=lambda value: value['target_rad'][0]))],
        })
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('log', type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(summarize(args.log), indent=2, ensure_ascii=False, allow_nan=False))
    except (OSError, ValueError) as error:
        parser.exit(1, f'Report failed: {error}\n')


if __name__ == '__main__':
    main()
