"""What the bridge answers: one request line in, one reply (a dict) out. No sockets here (see server.py).

The official commands (`help`, `pos`, `state`) answer with the field names, units and error texts of TotK Recomp's
control socket (docs/control.md, runtime/control.c there). Commands that socket doesn't have start with `x-`, so they
never clash with one it adds later.

`help` also carries a `bridge` object the official socket doesn't have: a client tells the two apart by it and checks
`protocol` (this module's reply shapes) and `build_ok` (whether the running game is the build the reader knows)
before it trusts any data.
"""
import base64
import math
import struct
import time

from . import PROTOCOL, __version__
from .reader import GAME_VERSION, SUPPORTED, WORLD_SCENE, LiveError, LiveReader

NO_SAVE = 'No save loaded'   # the official socket's text for the title screen and while a save loads


def yaw_from_rotation(rot):
    """Yaw in degrees (0 along +z, 90 along +x) from the Player actor's 3x3 rotation (rows in memory order), or None
    when it isn't a turn about y. Row 0 is (cos a, 0, sin a), as runtime/control.c measures it."""
    if not rot:
        return None
    c, s = rot[0], rot[2]
    n = c * c + s * s
    if not (math.isfinite(n) and 0.5 < n < 1.5):
        return None
    return round(math.degrees(math.atan2(s, c)), 1)


def yaw_to_heading(yaw):
    """A map bearing: clockwise from north (-z), in [0, 360)."""
    return round((180.0 - yaw) % 360.0, 1)


def is_shrine(scene):
    return bool(scene) and scene != WORLD_SCENE


COMMANDS = [
    ('help', '', 'the commands, and the bridge: its version, protocol and whether it can read this game build'),
    ('pos', '', "Link's world position x, y, z, yaw and heading, as the official pos (in a shrine: the shrine's "
                "own coordinates; x-scene tells)"),
    ('state', '', 'game (version), uptime_s, world_s (game time since the save loaded, left out before), in_world: '
                  'the fields of the official state the bridge can know'),
    ('x-scene', '', "scene: 'MainField' (the open world) or the shrine Link is in ('Dungeon062'); shrine: true in one"),
    ('x-flags', '', 'the GameData flags and the set GUIDs, as in progress.sav: flags = base64 of <II (hash, value) '
                    'pairs, guids = base64 of <Q'),
    ('x-inventory', '', 'armor: the armor actors carried; materials: {actor: count}'),
]


def ok(data):
    return {'ok': True, 'data': data}


def err(text):
    return {'ok': False, 'error': text}


class Bridge:
    """Answers request lines. Thread-safe (the reader locks)."""

    def __init__(self, reader=None):
        self.reader = reader or LiveReader()
        self.started = time.monotonic()

    def info(self):
        st = self.reader.status()
        return {'name': 'totk-live-bridge', 'version': __version__, 'protocol': PROTOCOL, 'supported': SUPPORTED,
                'available': st['available'], 'build_ok': st['build_ok'], 'error': st['error']}

    def handle(self, line):
        parts = line.strip().split()
        if not parts:
            return err('empty request')
        cmd = parts[0].lower()
        fn = getattr(self, 'cmd_' + cmd.replace('-', '_'), None)
        if fn is None or cmd not in {c[0] for c in COMMANDS}:
            return err(f'unknown command: {cmd}')
        try:
            return ok(fn())
        except LiveError as e:
            return err(str(e))

    # ---- official commands
    def cmd_help(self):
        return {'commands': [{'name': n, 'args': a, 'help': h} for n, a, h in COMMANDS], 'bridge': self.info()}

    def _pose(self):
        (x, y, z), rot = self.reader.player()
        scene = self.reader.scene()
        # the title screen keeps the Player at the origin (control.c checks it the same way); a shrine's own
        # coordinates can be near 0, so only in the open world
        if not is_shrine(scene) and abs(x) + abs(z) < 1:
            raise LiveError(NO_SAVE)
        return (x, y, z), yaw_from_rotation(rot), scene

    def cmd_pos(self):
        (x, y, z), yaw, _ = self._pose()
        out = {'x': round(x, 2), 'y': round(y, 2), 'z': round(z, 2)}
        place = f'totk:1 x={x:.2f} y={y:.2f} z={z:.2f}'
        if yaw is not None:
            out.update(yaw=yaw, heading=yaw_to_heading(yaw))
            place += f' yaw={yaw:.1f}'
        out['place'] = place
        return out

    def cmd_state(self):
        st = self.reader.status()
        if not st['available']:
            raise LiveError(st['error'])
        out = {'game': GAME_VERSION, 'uptime_s': round(time.monotonic() - self.started, 1)}
        try:
            w = self.reader.world_us()
        except LiveError:
            w = None
        if w is not None:   # as the official state: left out before the first save has loaded
            out['world_s'] = round(w / 1e6, 1)
        try:
            self._pose()
            out['in_world'] = True
        except LiveError:
            out['in_world'] = False
        return out

    # ---- extensions
    def cmd_x_scene(self):
        scene = self.reader.scene()
        return {'scene': scene, 'shrine': is_shrine(scene)}

    def cmd_x_flags(self):
        flags, guids = self.reader.snapshot()
        packed = b''.join(struct.pack('<II', h, v) for h, v in sorted(flags.items()))
        return {'count': len(flags), 'flags': base64.b64encode(packed).decode('ascii'),
                'guid_count': len(guids), 'guids': base64.b64encode(struct.pack(f'<{len(guids)}Q', *sorted(guids))).decode('ascii')}

    def cmd_x_inventory(self):
        armor, mats = self.reader.inventory()
        return {'armor': sorted(armor), 'materials': dict(sorted(mats.items()))}


def decode_flags(data):
    """The other side of x-flags: ({hash: value}, {guid})."""
    raw = base64.b64decode(data['flags'])
    graw = base64.b64decode(data['guids'])
    flags = dict(struct.iter_unpack('<II', raw))
    guids = {g for (g,) in struct.iter_unpack('<Q', graw)}
    if len(flags) != data.get('count', len(flags)) or len(guids) != data.get('guid_count', len(guids)):
        raise ValueError('x-flags reply is cut short')
    return flags, guids
