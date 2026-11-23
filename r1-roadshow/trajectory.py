"""Solve the whole optical path before producing a joint playback file."""
import math
from dataclasses import dataclass

import mujoco
import numpy as np
from scipy.interpolate import CubicSpline
from scipy.optimize import least_squares
from scipy.spatial.transform import Rotation, Slerp

import presets

FPS = 100
POSITION_TOL = 0.0015
ANGLE_TOL_DEG = 0.5
STOP_MARGIN = 0.012
UP = np.array([0., 0., 1.])


@dataclass
class Waypoint:
    name: str
    position: np.ndarray
    rotation: Rotation
    transition: float
    hold: float


class Planner:
    def __init__(self, model_path, profile):
        self.profile = profile
        mode = profile['mode']
        if mode not in {'simulation', 'field'}:
            raise ValueError('mode must be simulation or field')
        camera = profile['camera']
        offset = self.checked_vector(camera['link7_xyz_m'], 3, 'camera.xyz')
        self.camera_rotation = Rotation.from_euler(
            'xyz', self.checked_vector(camera['link7_rpy_deg'], 3, 'camera.rpy_deg'), degrees=True)
        self.fov = self.checked_vector(
            [camera['horizontal_fov_deg'], camera['vertical_fov_deg']], 2, 'camera field of view')
        if np.any(self.fov <= 0) or np.any(self.fov >= 170):
            raise ValueError('camera field of view must be between 0 and 170 degrees')

        spec = mujoco.MjSpec.from_file(str(model_path))
        if spec.modelname != 'unitree_r1_7a_right':
            raise ValueError('expected the right R1-7A model')
        for parent, child in zip(('base_link', *(f'Link{i}' for i in range(1, 7))),
                                 (f'Link{i}' for i in range(1, 8))):
            spec.add_exclude(bodyname1=parent, bodyname2=child)
        # The compact wrist's official convex Link5/7 hulls overlap at neutral.
        spec.add_exclude(bodyname1='Link5', bodyname2='Link7')
        if 'base_xyz_m' in profile:
            spec.body('base_link').pos = self.checked_vector(profile['base_xyz_m'], 3, 'base position')
        wrist = spec.body('Link7')
        wrist.add_site(name='roadshow_lens', pos=offset, size=[0.01, 0.01, 0.01])
        radius = float(camera['clearance_radius_m'])
        if not 0.001 <= radius <= 0.2:
            raise ValueError('camera clearance sphere must have radius within 0.001–0.2 m')
        wrist.add_geom(name='camera_clearance', type=mujoco.mjtGeom.mjGEOM_SPHERE,
                       pos=offset, size=[radius, 0, 0], mass=0,
                       contype=4, conaffinity=2)
        # Check the existing solid collision hulls against nonadjacent links and the floor.
        # Link 5/6/7 and the camera mount naturally overlap near the wrist.
        for geom in spec.geoms:
            if geom.name.startswith('collision_'):
                near_wrist = geom.name.startswith(('collision_Link5_', 'collision_Link6_', 'collision_Link7_'))
                geom.conaffinity = 3 if near_wrist else 7
            elif geom.name == 'floor':
                geom.conaffinity = 5
        self.boxes = []
        for index, box in enumerate(profile.get('obstacles', [])):
            center = self.checked_vector(box['center_m'], 3, 'obstacle center')
            half_size = self.checked_vector(box['size_m'], 3, 'obstacle size') / 2
            if np.any(half_size <= 0):
                raise ValueError('obstacle dimensions must be positive')
            self.boxes.append((center - half_size, center + half_size))
            spec.worldbody.add_geom(name=f'venue_{index}', type=mujoco.mjtGeom.mjGEOM_BOX,
                                    pos=center, size=half_size, contype=2, conaffinity=5)
        self.model = spec.compile()
        self.data = mujoco.MjData(self.model)
        if (self.model.nq != 7 or self.model.nv != 7 or
                [self.model.joint(i).name for i in range(7)] != [f'J{i}' for i in range(1, 8)]):
            raise ValueError('expected the fixed-base right R1-7A model with joints ordered J1 to J7')
        self.site = self.model.site('roadshow_lens').id
        bounds = self.model.jnt_range.copy()
        # Published R1-7A J6 range is narrower than the available right-side URDF.
        bounds[5] = np.clip(bounds[5], -math.pi / 6, math.pi / 6)
        if mode == 'field':
            field = np.asarray(profile['joint_bounds_rad'], dtype=float)
            if (field.shape != (7, 2) or not np.all(np.isfinite(field)) or
                    np.any(field[:, 0] < bounds[:, 0] - 1e-8) or np.any(field[:, 1] > bounds[:, 1] + 1e-8)):
                raise ValueError('field joint bounds must fit the seven model limits')
            bounds = field
        self.low, self.high = bounds[:, 0], bounds[:, 1]
        if np.any(self.high - self.low < 2 * STOP_MARGIN):
            raise ValueError('joint ranges leave no setpoint margin')
        self.vel = self.checked_vector(profile['limit_rad_s'], 7, 'joint velocity limits')
        self.acc = self.checked_vector(profile['limit_rad_s2'], 7, 'joint acceleration limits')
        if np.any(self.vel <= 0) or np.any(self.vel > 0.25) or np.any(self.acc <= 0) or np.any(self.acc > 0.5):
            raise ValueError('roadshow caps are 0.25 rad/s and 0.5 rad/s²')
        self.start = self.joints(profile['start_rad'], 'start_rad')
        self.position_error = 0.
        self.orientation_error = 0.
        self.section_times = []

    @staticmethod
    def checked_vector(raw, length, name):
        vector = np.asarray(raw, dtype=float)
        if vector.shape != (length,) or not np.all(np.isfinite(vector)):
            raise ValueError(f'{name}: expected {length} finite numbers')
        return vector

    def joints(self, raw, name):
        q = self.checked_vector(raw, 7, name)
        if np.any(q < self.low + STOP_MARGIN) or np.any(q > self.high - STOP_MARGIN):
            raise ValueError(f'{name}: joints outside configured limits or too close to edge')
        self.check_geometry(q, name)
        return q

    def set_q(self, q):
        self.data.qpos[:] = q
        mujoco.mj_kinematics(self.model, self.data)

    def optical_pose(self, q):
        self.set_q(q)
        return (self.data.site_xpos[self.site].copy(),
                Rotation.from_matrix(self.data.site_xmat[self.site].reshape(3, 3)) * self.camera_rotation)

    def check_geometry(self, q, name):
        self.data.qpos[:] = q
        mujoco.mj_fwdPosition(self.model, self.data)
        for i in range(self.data.ncon):
            contact = self.data.contact[i]
            if contact.dist < -0.0005:
                first, second = (self.model.geom(g).name for g in contact.geom)
                raise ValueError(f'{name}: model collision {first} / {second} ({contact.dist * 1000:.2f} mm)')

    def shot_targets(self, preset, narration_holds):
        if preset == 'PortraitSearch':
            category = 'search'
            steps = presets.SEARCH
            library = presets.SEARCH_DELTA
        elif preset in presets.ROADSHOW:
            category = 'idle' if preset in ('RoadshowIdleScan', 'RoadshowStraightLateral') else 'seated'
            steps = [(name, move, hold) for name, move, hold, _ in presets.ROADSHOW[preset]]
            library = {name: pose for name, _, _, pose in presets.ROADSHOW[preset]}
        else:
            category = 'standing' if preset == 'PortraitStandingPreset' else 'seated'
            steps = [(name, 1.8, 0.8) for name in presets.TEMPLATES[preset]]
            library = presets.PORTRAIT_DELTA
        if preset == 'PortraitNarrativeShowcasePreset':
            if narration_holds is None or len(narration_holds) != len(steps):
                raise ValueError('supply one narration duration per step with --narration-holds')
            if any(not math.isfinite(seconds) or not 0 < seconds <= 120 for seconds in narration_holds):
                raise ValueError('narration durations must be positive and at most 120 seconds')
            steps = [(name, move, max(hold, voice)) for (name, move, hold), voice in zip(steps, narration_holds)]
        if category in ('search', 'idle'):
            q = self.start
            distance = float(self.profile.get('search_range_m', 0.5))
        else:
            anchor = self.profile['anchors'][category]
            q = self.joints(anchor['q_rad'], f'{category} anchor')
            distance = float(anchor['target_range_m'])
        if not math.isfinite(distance) or distance < 0.15:
            raise ValueError('target range must be at least 0.15 m')
        origin, attitude = self.optical_pose(q)
        forward = attitude.apply([1, 0, 0])
        heading = math.atan2(forward[1], forward[0])
        horiz_forward = np.array([math.cos(heading), math.sin(heading), 0.])
        left = np.cross(UP, horiz_forward)
        travel_scale = float(self.profile.get('travel_scale', 1))
        if not math.isfinite(travel_scale) or not 0 < travel_scale <= 4:
            raise ValueError('travel_scale must be greater than 0 and at most 4')
        targets = []
        for name, move, hold in steps:
            x, y, z, pitch = library[name]
            x, y, z = np.array([x, y, z]) * travel_scale
            # The old base servo naturally turns toward its lateral offset.
            # Portrait keeps the taught subject in view. Search points
            # outward as the original scan did.
            lateral_yaw = math.atan2(y * (1 if category in ('search', 'idle') else -1), distance - x)
            if preset == 'RoadshowStraightLateral':
                lateral_yaw = 0.
            target_attitude = (Rotation.from_rotvec(UP * lateral_yaw) * attitude *
                               Rotation.from_euler('y', pitch * travel_scale, degrees=True))
            targets.append(Waypoint(name, origin + x * horiz_forward + y * left + z * UP,
                                    target_attitude, move, hold))
        return targets

    @staticmethod
    def smooth(u):
        return u**3 * (10 + u * (-15 + 6 * u))

    def solve_path(self, start_q, end_position, end_rotation, name):
        start_position, start_rotation = self.optical_pose(start_q)
        interpolate_rotation = Slerp([0, 1], Rotation.concatenate([start_rotation, end_rotation]))
        samples = np.linspace(0, 1, 33)
        positions = start_position + samples[:, None] * (end_position - start_position)
        orientations = interpolate_rotation(samples)
        solved = [start_q]
        for value, position, orientation in zip(samples[1:], positions[1:], orientations[1:]):
            previous = solved[-1]
            inverse = orientation.inv()

            def residual(q):
                actual_p, actual_r = self.optical_pose(q)
                return np.r_[actual_p - position,
                             0.16 * (inverse * actual_r).as_rotvec(),
                             0.0015 * (q - previous)]

            q = least_squares(residual, previous, bounds=(
                np.maximum(self.low + STOP_MARGIN, previous - 0.08),
                np.minimum(self.high - STOP_MARGIN, previous + 0.08)),
                xtol=1e-7, ftol=1e-7, gtol=1e-8, max_nfev=70).x
            actual_p, actual_r = self.optical_pose(q)
            if (np.linalg.norm(actual_p - position) > POSITION_TOL or
                    (inverse * actual_r).magnitude() > math.radians(ANGLE_TOL_DEG)):
                raise ValueError(f'{name}: continuous camera IK failed at {value:.0%}; change anchor or scale')
            solved.append(q)
        return CubicSpline(samples, np.array(solved), axis=0), start_position, interpolate_rotation

    def time_section(self, interpolate, seconds, name):
        if not math.isfinite(seconds) or not 0 < seconds <= 120:
            raise ValueError(f'{name}: transition must be in (0, 120] seconds')
        ticks = max(2, math.ceil(seconds * FPS))
        for _ in range(10):
            u = np.linspace(0, 1, ticks + 1)
            if ticks > 12000:
                break
            q = interpolate(self.smooth(u))
            speed = np.diff(q, axis=0) * FPS
            acc = np.diff(np.vstack((np.zeros((1, 7)), speed, np.zeros((1, 7)))), axis=0) * FPS
            stretch = max(float(np.max(np.abs(speed) / self.vel)),
                          float(np.sqrt(np.max(np.abs(acc) / self.acc))))
            if stretch <= 0.995:
                if np.any(q < self.low + STOP_MARGIN - 1e-8) or np.any(q > self.high - STOP_MARGIN + 1e-8):
                    raise ValueError(f'{name}: interpolated path exceeds the usable joint range')
                for sample in q:
                    self.check_geometry(sample, name)
                return q, ticks / FPS, self.smooth(u)
            ticks = math.ceil(ticks * stretch * 1.02)
        raise ValueError(f'{name}: unable to meet velocity and acceleration limits')

    def camera_move(self, q, waypoint):
        path, start_position, attitude = self.solve_path(q, waypoint.position, waypoint.rotation, waypoint.name)
        states, seconds, blend = self.time_section(path, waypoint.transition, waypoint.name)
        target_r = attitude(blend)
        target_p = start_position + blend[:, None] * (waypoint.position - start_position)
        for state, position, rotation in zip(states, target_p, target_r):
            actual_p, actual_r = self.optical_pose(state)
            pos_err = float(np.linalg.norm(actual_p - position))
            rot_err = math.degrees(float((rotation.inv() * actual_r).magnitude()))
            if pos_err > POSITION_TOL or rot_err > ANGLE_TOL_DEG:
                raise ValueError(f'{waypoint.name}: resampled camera pose exceeds tracking tolerance')
            self.position_error = max(self.position_error, pos_err)
            self.orientation_error = max(self.orientation_error, rot_err)
        return states, seconds

    def taught_move(self, q, target, name, seconds):
        return self.time_section(lambda t: q + t[:, None] * (target - q), seconds, name)[:2]

    def add_section(self, rows, name, states, seconds, hold):
        if not math.isfinite(hold) or not 0 <= hold <= 120:
            raise ValueError(f'{name}: hold must be in 0–120 seconds')
        ticks = math.ceil(hold * FPS)
        start_seconds = (len(rows) - 1) / FPS
        rows.extend(states[1:])
        rows.extend([states[-1]] * ticks)
        position, orientation = self.optical_pose(states[-1])
        self.section_times.append({
            'name': name, 't_start_s': start_seconds, 'move_s': seconds, 'hold_s': ticks / FPS,
            'camera_xyz_m': position.tolist(), 'camera_rpy_deg': orientation.as_euler('xyz', degrees=True).tolist(),
            'q_rad': states[-1].tolist(),
        })

    def coverage(self):
        targets = {name: self.checked_vector(point, 3, name)
                   for name, point in self.profile.get('coverage_points_m', {}).items()}
        visible = {name: [] for name in targets}
        simultaneous = []
        tan_fov = np.tan(np.deg2rad(self.fov / 2))
        for section in self.section_times:
            visible_here = []
            rotation = Rotation.from_euler('xyz', section['camera_rpy_deg'], degrees=True)
            for name, target in targets.items():
                local = rotation.inv().apply(target - section['camera_xyz_m'])
                if local[0] <= 0:
                    continue
                screen = np.abs(local[1:] / local[0]) / tan_fov
                if np.all(screen <= 0.9) and not self.obstructed(section['camera_xyz_m'], target):
                    visible_here.append(name)
                    visible[name].append(section['name'])
            if targets and len(visible_here) == len(targets):
                simultaneous.append(section['name'])
        return visible, simultaneous

    def obstructed(self, origin, target):
        # Intersect the lens-to-marker segment with surveyed boxes.
        origin = np.asarray(origin)
        direction = target - origin
        for low, high in self.boxes:
            enter, leave = 0., 1.
            for point, delta, lower, upper in zip(origin, direction, low, high):
                if abs(delta) < 1e-9:
                    if not lower <= point <= upper:
                        leave = -1.
                        break
                    continue
                first, last = sorted(((lower - point) / delta, (upper - point) / delta))
                enter, leave = max(enter, first), min(leave, last)
            if enter < leave and enter < 0.999:
                return True
        return False

    def return_path(self, rows):
        try:
            q, _ = self.taught_move(rows[-1], self.start, 'return to supported start', 1.5)
            # Force the exact final encoded coordinates.
            q[-1] = self.start
            return q, 'direct'
        except ValueError:
            # A shortcut can cross the desk or the arm. Reverse the checked
            # transits, preserving each segment's timing and dropping holds.
            reverse = [np.asarray(rows[:1])]
            for section in self.section_times:
                start = round(section['t_start_s'] * FPS)
                end = start + round(section['move_s'] * FPS)
                reverse.append(np.asarray(rows[start+1:end+1]))
            return np.concatenate(reverse)[::-1], 'retrace'

    def make(self, preset, narration_holds):
        self.section_times = []
        self.position_error = self.orientation_error = 0.
        rows = [self.start]
        if preset == 'VenueTour':
            venue = self.profile['venue']
            if not venue:
                raise ValueError('VenueTour requires taught venue poses')
            for mark in venue:
                name = mark['name']
                target = self.joints(mark['q_rad'], name)
                states, seconds = self.taught_move(rows[-1], target, name, float(mark.get('move_s', 1.8)))
                self.add_section(rows, name, states, seconds, float(mark['hold_s']))
        else:
            for waypoint in self.shot_targets(preset, narration_holds):
                states, seconds = self.camera_move(rows[-1], waypoint)
                self.add_section(rows, waypoint.name, states, seconds, waypoint.hold)
        coverage, simultaneous = self.coverage() if preset == 'VenueTour' else ({}, [])
        motion_seconds = (len(rows) - 1) / FPS

        recovery, return_kind = self.return_path(rows)
        rows.extend(recovery[1:])
        if len(rows) > 120000:
            raise ValueError('trajectory exceeds 1200 seconds')
        report = {
            'preset': preset, 'mode': self.profile['mode'], 'rate_hz': FPS,
            'motion_s': motion_seconds, 'return_s': (len(recovery) - 1) / FPS,
            'return_path': return_kind,
            'max_position_error_mm': self.position_error * 1000,
            'max_orientation_error_deg': self.orientation_error,
            'coverage_points_in_frame': coverage,
            'uncovered_points': [name for name, views in coverage.items() if not views],
            'simultaneous_full_coverage_views': simultaneous,
            'waypoints': self.section_times,
        }
        return np.asarray(rows), report
