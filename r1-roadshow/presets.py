"""Camera-space recipes transcribed from the ArmPi Ultra preset libraries."""

PORTRAIT = {
    'PortraitDistanceFollowPreset': ('center_far_tracking', 'center_medium_tracking', 'center_near_tracking'),
    'PortraitBiasSwitchPreset': ('left_bias_20_medium_tracking', 'center_medium_tracking', 'right_bias_20_medium_tracking'),
    'PortraitCloseFocusPreset': ('center_medium_tracking', 'center_near_tracking', 'center_medium_tracking'),
    'PortraitShowcasePreset': (
        'center_far_tracking', 'center_medium_tracking', 'left_bias_20_medium_tracking',
        'center_near_tracking', 'right_bias_20_medium_tracking', 'center_medium_tracking',
    ),
    'PortraitStandingPreset': (
        'standing_center_medium_tracking', 'standing_center_near_tracking',
        'standing_center_medium_tracking', 'standing_center_far_tracking', 'standing_center_medium_tracking',
    ),
}

PORTRAIT_DESIGN = {
    'PortraitBiasPulsePreset': (
        'center_medium_tracking', 'left_bias_15_medium_tracking', 'center_medium_tracking',
        'right_bias_15_medium_tracking', 'center_medium_tracking',
    ),
    'PortraitExtendedShowcasePreset': (
        'center_far_tracking', 'left_bias_20_medium_tracking', 'center_medium_tracking',
        'right_bias_20_medium_tracking', 'center_near_tracking', 'center_medium_tracking', 'center_far_tracking',
    ),
    'PortraitNarrativeShowcasePreset': PORTRAIT['PortraitShowcasePreset'],
}

SEARCH = (
    ('search_extend_center', 1.4, 0.5),
    ('search_extend_left', 1.0, 0.5),
    ('search_extend_right', 1.4, 0.5),
    ('search_extend_center', 1.0, 0.0),
)

# (forward, left, up) in meters, followed by added camera pitch-down in degrees.
# After the source's X clamp at the nominal x=0.150 and its nominal z/pitch.
PORTRAIT_DELTA = {
    'center_far_tracking': (-0.025, 0, 0, -3.5),
    'center_medium_tracking': (0, 0, 0, 0),
    'center_near_tracking': (0.025, 0, 0.018, 2.2),
    'left_bias_20_medium_tracking': (0, 0.030, 0, 0),
    'right_bias_20_medium_tracking': (0, -0.030, 0, 0),
    'left_bias_15_medium_tracking': (0, 0.0225, 0, 0),
    'right_bias_15_medium_tracking': (0, -0.0225, 0, 0),
    'standing_center_far_tracking': (-0.025, 0, 0, -3.5),
    'standing_center_medium_tracking': (0, 0, 0, 0),
    'standing_center_near_tracking': (0.025, 0, 0.018, 2.2),
}

# Relative to the original home pose (0.145, 0, 0.158, 22°).
SEARCH_DELTA = {
    'search_extend_center': (0.005, 0, 0.102, -17),
    'search_extend_left': (0.005, 0.045, 0.102, -17),
    'search_extend_right': (0.005, -0.045, 0.102, -17),
}

DESIGN_REQUIRES_VISION = {
    'PortraitRecenterPreset': 'CenterTarget between distance moves',
    'PortraitFollowAndRecoverPreset': 'face loss, identity-preserving reacquisition',
    'PortraitSitToStandPreset': 'pose-transition detection and seated/standing handoff',
    'PortraitLightWalkPreset': 'walking subject tracking and pose fallback',
    'PortraitDynamicHeadroomPreset': 'live nose anchor and adaptive headroom',
    'PortraitDynamicChestPreset': 'live shoulder anchor',
    'PortraitAdaptiveSitToStandPreset': 'live nose/shoulder switching through the rise',
}

TEMPLATES = PORTRAIT | PORTRAIT_DESIGN

# Roadshow capture routines and the fixed-heading lateral test.
# +Y points left in the camera's horizontal frame.
ROADSHOW = {
    'RoadshowIdleScan': (
        ('idle_left', 1.8, 2.0, (0, 0.05, 0, 0)),
        ('idle_right', 1.8, 2.0, (0, -0.05, 0, 0)),
        ('idle_up', 1.8, 2.0, (0, 0, 0, -3)),
        ('idle_down', 1.8, 2.0, (0, 0, 0, 5)),
    ),
    'RoadshowCaptureSweep': (
        ('capture_left', 1.8, 0.0, (0, 0.04, 0, 0)),
        ('capture_right', 7.0, 0.0, (0, -0.04, 0, 0)),
    ),
    'RoadshowCapturePush': (
        ('capture_far', 1.8, 0.0, (-0.02, 0, 0.02, 0)),
        ('capture_near', 6.0, 0.0, (0.02, 0, -0.02, 0)),
    ),
    'RoadshowStraightLateral': (
        ('straight_left', 3.5, 0.5, (0, 0.020, 0, 0)),
        ('straight_right', 7.0, 0.5, (0, -0.020, 0, 0)),
        ('straight_center', 3.5, 0.5, (0, 0, 0, 0)),
    ),
}

AUXILIARY = ('PortraitSearch', 'VenueTour', *ROADSHOW)
