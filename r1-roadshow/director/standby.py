"""The approved physical start and its separate simulation representation."""
import hashlib
import json
import math
from pathlib import Path


FIELD = Path(__file__).resolve().parent.parent / 'field'


def joint_vector(values):
    if (len(values) != 7 or any(type(value) not in (int, float) or not math.isfinite(value)
                                for value in values)):
        raise ValueError('Expected seven finite joint angles')
    return list(values)


def approved_start():
    reference = (FIELD / 'standby-reference.txt').read_bytes()
    calibration_hash, sdk, kind, motor = reference.decode().splitlines()
    snapshot = json.loads((FIELD / 'standby-home.json').read_text())
    sdk = joint_vector([float(angle) for angle in sdk.split()])
    motor = joint_vector([float(angle) for angle in motor.split()])
    if (kind != 'standby' or snapshot['status'] != 'approved_powered_standby'
            or calibration_hash != snapshot['calibration_sha256']
            or snapshot['mode'] != 1 or snapshot['stable_s'] != 5
            or not all(math.isclose(recorded, saved, abs_tol=1e-8, rel_tol=0)
                       for reference_angles, key in ((sdk, 'q_sdk_rad'), (motor, 'q_motor_rad'))
                       for recorded, saved in zip(reference_angles, joint_vector(snapshot[key]), strict=True))):
        raise ValueError('Approved standby reference does not match the recorded pose')
    return {
        'reference': 'field/standby-reference.txt',
        'reference_sha256': hashlib.sha256(reference).hexdigest(),
        'captured_at_utc': snapshot['captured_at_utc'],
        'calibration_sha256': calibration_hash,
        'q_sdk_rad': sdk,
        'q_motor_rad': motor,
        'open_from_park_joint_order': ['J4', 'J2'],
        'stable_s': snapshot['stable_s'],
    }


def check_start_frames(data, name, model_start, hold_s, calibration_hash=None):
    lines = data.splitlines()
    header = lines[0].split() if lines else []
    mode = b'field' if calibration_hash else b'simulation'
    digest = calibration_hash.encode() if calibration_hash else b'-'
    if header[:4] != [b'R1TRAJ1', mode, b'100', digest] or len(header) != 6:
        raise ValueError(f'{name}: expected a 100 Hz {mode.decode()} plan with the matching calibration')
    first_frame = 8 if calibration_hash else 5
    hold_frames = hold_s * 100 + 1
    start = [f'{angle:.9f}'.encode() for angle in model_start]
    if (len(lines) != first_frame + int(header[4]) or len(lines) <= first_frame + hold_frames
            or any(line.split() != start for line in lines[first_frame:first_frame + hold_frames])
            or lines[-1].split() != start):
        raise ValueError(f'{name}: expected {hold_s}s at the common start and a return to it')
