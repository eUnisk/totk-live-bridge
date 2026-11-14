"""python -m unittest discover -s tests (from the repository root). No game needed: a fake reader stands in."""
import base64
import io
import json
import math
import struct
import socket
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from totk_live_bridge import PROTOCOL, __version__  # noqa: E402
from totk_live_bridge.protocol import Bridge, decode_flags  # noqa: E402
from totk_live_bridge.reader import LiveError, UnsupportedBuild, pe_static  # noqa: E402
from totk_live_bridge.server import MAX_CONNECTIONS, MAX_LINE, BridgeServer  # noqa: E402


def turn(deg):
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    return (c, 0.0, s, 0.0, 1.0, 0.0, -s, 0.0, c)


class FakeReader:
    def __init__(self, pos=(843.4, 119.1, 849.6), rot=turn(-136.3), scene='MainField', fail=None,
                 world=3815578793):
        self.pos, self.rot, self._scene, self.fail, self.world = pos, rot, scene, fail, world

    def _check(self):
        if self.fail:
            raise self.fail

    def player(self):
        self._check()
        return self.pos, self.rot

    def scene(self):
        self._check()
        return self._scene

    def snapshot(self):
        self._check()
        return {0xE573F564: 12601, 7: 1}, {3, 0xFFFFFFFFFFFFFFFF}

    def world_us(self):
        self._check()
        if isinstance(self.world, Exception):
            raise self.world
        return self.world

    def inventory(self):
        self._check()
        return {'Armor_001_Head'}, {'Item_Fruit_A': 16}

    def status(self):
        if self.fail:
            return {'available': False, 'pid': None, 'build_ok': False if isinstance(self.fail, UnsupportedBuild) else None,
                    'error': str(self.fail)}
        return {'available': True, 'pid': 1, 'build_ok': True, 'error': None}


class ProtocolTest(unittest.TestCase):
    def test_pos_matches_official(self):
        # the official example: {"x":843.40,"y":119.10,"z":849.60,"yaw":-136.3,"heading":316.3,"place":...}
        r = Bridge(FakeReader()).handle('pos')
        self.assertEqual(r, {'ok': True, 'data': {
            'x': 843.4, 'y': 119.1, 'z': 849.6, 'yaw': -136.3, 'heading': 316.3,
            'place': 'totk:1 x=843.40 y=119.10 z=849.60 yaw=-136.3'}})

    def test_title_screen(self):
        self.assertEqual(Bridge(FakeReader(pos=(0.0, 0.0, 0.0))).handle('pos'), {'ok': False, 'error': 'No save loaded'})
        self.assertFalse(Bridge(FakeReader(pos=(0.0, 0.0, 0.0))).handle('state')['data']['in_world'])

    def test_shrine(self):
        b = Bridge(FakeReader(pos=(0.2, -6.3, 0.3), scene='Dungeon062'))
        self.assertTrue(b.handle('pos')['ok'])   # a shrine's own coordinates can be near 0
        self.assertEqual(b.handle('x-scene')['data'], {'scene': 'Dungeon062', 'shrine': True})

    def test_help_handshake(self):
        d = Bridge(FakeReader()).handle('help')['data']
        names = [c['name'] for c in d['commands']]
        self.assertEqual(names[:3], ['help', 'pos', 'state'])
        self.assertTrue(all(n.startswith('x-') for n in names[3:]))
        self.assertEqual((d['bridge']['protocol'], d['bridge']['version'], d['bridge']['build_ok']),
                         (PROTOCOL, __version__, True))

    def test_state_world_clock(self):
        d = Bridge(FakeReader()).handle('state')['data']
        self.assertEqual(list(d), ['game', 'uptime_s', 'world_s', 'in_world'])   # the official order
        self.assertEqual((d['game'], d['world_s'], d['in_world']), ('1.4.3', 3815.6, True))
        # before the first save has loaded (and when the clock can't be read) world_s is left out, as officially
        for world in (None, LiveError('cannot read 0x8 bytes')):
            d = Bridge(FakeReader(pos=(0.0, 0.0, 0.0), world=world)).handle('state')['data']
            self.assertNotIn('world_s', d)
            self.assertFalse(d['in_world'])

    def test_flags_round_trip(self):
        d = Bridge(FakeReader()).handle('x-flags')['data']
        self.assertEqual(decode_flags(d), ({0xE573F564: 12601, 7: 1}, {3, 0xFFFFFFFFFFFFFFFF}))

    def test_inventory(self):
        self.assertEqual(Bridge(FakeReader()).handle('x-inventory')['data'],
                         {'armor': ['Armor_001_Head'], 'materials': {'Item_Fruit_A': 16}})

    def test_unsupported_build(self):
        b = Bridge(FakeReader(fail=UnsupportedBuild('unsupported game build (table 0 vtable 0x1)')))
        self.assertEqual(b.handle('help')['data']['bridge']['build_ok'], False)
        for cmd in ('pos', 'state', 'x-flags', 'x-inventory', 'x-scene'):
            r = b.handle(cmd)
            self.assertFalse(r['ok'])
            self.assertIn('unsupported game build', r['error'])

    def test_game_not_running(self):
        b = Bridge(FakeReader(fail=LiveError('totk.exe is not running')))
        self.assertIsNone(b.handle('help')['data']['bridge']['build_ok'])
        self.assertEqual(b.handle('pos'), {'ok': False, 'error': 'totk.exe is not running'})

    def test_unknown(self):
        b = Bridge(FakeReader())
        self.assertEqual(b.handle('teleport'), {'ok': False, 'error': 'unknown command: teleport'})
        self.assertEqual(b.handle('__init__')['ok'], False)
        self.assertEqual(b.handle('')['ok'], False)


def make_pe(symbols, buildid=b'\x01' * 0x35):
    """A PE file with the sections .text, .data, .bss (and .buildid) and a COFF symbol table of
    (name, section number, value, storage class) records, each followed by one aux record of zeros."""
    sections = [(b'.text', 0x1000, 0x100), (b'.data', 0x2000, 0x100), (b'.bss', 0x3000, 0x100)]
    if buildid is not None:
        sections.append((b'.buildid', 0x4000, len(buildid)))
    pe, optsz = 0x80, 0xf0
    raw = 0x400   # all section data starts here (only .buildid's is read)
    strings = b''
    table = b''
    for name, sec, value, cls in symbols:
        if len(name) <= 8:
            field = name.ljust(8, b'\0')
        else:
            field = struct.pack('<II', 0, 4 + len(strings))
            strings += name + b'\0'
        table += field + struct.pack('<IhHBB', value, sec, 0, cls, 1) + bytes(18)
    symptr = raw + 0x200
    fh = struct.pack('<HHIIIHH', 0x8664, len(sections), 0x12345678, symptr, 2 * len(symbols), optsz, 0x22)
    head = bytearray(raw)
    head[:2] = b'MZ'
    struct.pack_into('<I', head, 0x3c, pe)
    head[pe:pe + 24] = b'PE\0\0' + fh
    for i, (name, va, vsize) in enumerate(sections):
        struct.pack_into('<8sIIII', head, pe + 24 + optsz + 40 * i, name, vsize, va, 0x200 if name == b'.buildid' else 0, raw)
    body = (buildid or b'').ljust(0x200, b'\0')
    return bytes(head) + body + table + struct.pack('<I', 4 + len(strings)) + strings, fh


class ChangingReader(FakeReader):
    """A reader whose flags and set GUIDs the test changes between scans."""

    def __init__(self, flags, guids):
        super().__init__()
        self.flags, self.guids = dict(flags), set(guids)

    def snapshot(self):
        self._check()
        return dict(self.flags), set(self.guids)


def pull(bridge, since):
    """What a client does: pages with the same since until more is false. (next since, base, reset, flags, on, off)"""
    cursor, first, flags, on, off = 0, None, {}, [], []
    while True:
        d = bridge.handle(f'x-flags-since {since} {cursor}')['data']
        if first is None:
            first = d
        assert d['base'] == first['base']
        flags.update(struct.iter_unpack('<II', base64.b64decode(d['flags'])))
        on += [g for (g,) in struct.iter_unpack('<Q', base64.b64decode(d['guids_on']))]
        off += [g for (g,) in struct.iter_unpack('<Q', base64.b64decode(d['guids_off']))]
        if not d['more']:
            return first['seq'], first['base'], first['reset'], flags, on, off
        cursor = d['cursor']


class FlagsSinceTest(unittest.TestCase):
    def setUp(self):
        # 1500 flags: two pages; the GUIDs come after the flags, on the second page
        self.r = ChangingReader({h: h % 7 for h in range(1500)}, {11, 22})
        self.b = Bridge(self.r)
        self.b.log.seq = self.b.log.base = 5000   # as a bridge started at 5000 ms

    def scan(self):
        self.b.scanned = None   # the next request reads the game again

    def test_full_then_changes(self):
        seq, base, reset, flags, on, off = pull(self.b, 0)
        self.assertTrue(reset)
        self.assertEqual(flags, self.r.flags)
        self.assertEqual(sorted(on), [11, 22])
        self.assertEqual(off, [])
        self.assertEqual(base, 5001)

        self.r.flags[1234] = 99
        self.r.guids = {22, 33}
        self.scan()
        seq2, base2, reset, flags, on, off = pull(self.b, seq)
        self.assertFalse(reset)
        self.assertEqual((flags, on, off), ({1234: 99}, [33], [11]))
        self.assertEqual(base2, base)
        self.assertGreater(seq2, seq)

        self.scan()
        _, _, reset, flags, on, off = pull(self.b, seq2)
        self.assertEqual((reset, flags, on, off), (False, {}, [], []))

    def test_reset_sends_only_set_guids(self):
        seq, *_ = pull(self.b, 0)
        self.r.guids = {22}
        self.scan()
        pull(self.b, seq)
        _, _, reset, _, on, off = pull(self.b, 0)
        self.assertTrue(reset)
        self.assertEqual((on, off), ([22], []))   # 11 is known but unset: not in a reset

    def test_new_layout_resets(self):
        seq, base, *_ = pull(self.b, 0)
        self.r.flags[99999] = 1   # a flag appeared: the copy starts over
        self.scan()
        _, base2, reset, flags, _, _ = pull(self.b, seq)
        self.assertTrue(reset)
        self.assertGreater(base2, base)
        self.assertEqual(flags, self.r.flags)

    def test_since_from_another_run_resets(self):
        self.assertTrue(pull(self.b, 10 ** 15)[2])   # a since this bridge never gave

    def test_seq_starts_at_the_time(self):
        self.assertGreater(Bridge(FakeReader()).handle('x-flags-since 0')['data']['seq'], 1_000_000_000_000)

    def test_scans_at_most_every_stale(self):
        seq, *_ = pull(self.b, 0)
        self.r.flags[3] = 42
        d = self.b.handle(f'x-flags-since {seq}')['data']   # the copy is fresh: the change isn't seen yet
        self.assertEqual((d['seq'], d['count']), (seq, 0))

    def test_error_keeps_the_copy(self):
        seq, *_ = pull(self.b, 0)
        self.r.fail = LiveError('No save loaded')
        self.scan()
        self.assertEqual(self.b.handle(f'x-flags-since {seq}'), {'ok': False, 'error': 'No save loaded'})
        self.r.fail = None
        _, _, reset, flags, _, _ = pull(self.b, seq)
        self.assertEqual((reset, flags), (False, {}))

    def test_usage(self):
        usage = {'ok': False, 'error': 'Usage: x-flags-since <since> [cursor]'}
        for line in ('x-flags-since', 'x-flags-since -1', 'x-flags-since a', 'x-flags-since 1 2 3'):
            self.assertEqual(self.b.handle(line), usage, line)
        self.assertEqual(self.b.handle('pos now'), {'ok': False, 'error': 'Usage: pos'})
        args = {c['name']: c['args'] for c in self.b.handle('help')['data']['commands']}
        self.assertEqual((args['x-flags-since'], args['pos']), ('<since> [cursor]', ''))


class PeSymbolTest(unittest.TestCase):
    def test_short_name(self):
        data, fh = make_pe([(b'main', 1, 0x10, 2), (b'world_us', 2, 0x50, 3)])
        self.assertEqual(pe_static(io.BytesIO(data), b'world_us'), (0x2050, fh, (0x4000, b'\x01' * 0x35)))

    def test_long_name(self):
        data, _ = make_pe([(b'frame_step_world_us', 1, 0x40, 2), (b'some_long_clock_name', 3, 0x8, 3)], buildid=None)
        self.assertEqual(pe_static(io.BytesIO(data), b'some_long_clock_name')[::2], (0x3008, None))
        self.assertIsNone(pe_static(io.BytesIO(data), b'frame_step'))   # only whole names

    def test_not_a_variable(self):
        data, _ = make_pe([(b'world_us', 1, 0x50, 2)])   # in .text: a function, not the clock
        self.assertIsNone(pe_static(io.BytesIO(data), b'world_us'))
        data, _ = make_pe([(b'world_us', 2, 0x100, 3)])  # past the end of .data
        self.assertIsNone(pe_static(io.BytesIO(data), b'world_us'))
        self.assertIsNone(pe_static(io.BytesIO(b'not a PE file'), b'world_us'))


class SocketTest(unittest.TestCase):
    def setUp(self):
        self.server = BridgeServer(Bridge(FakeReader()), port=0).start()

    def tearDown(self):
        self.server.close()

    def connect(self):
        s = socket.create_connection(('127.0.0.1', self.server.port), timeout=3)
        return s, s.makefile('rb')

    def ask(self, s, f, line):
        s.sendall(line)
        return json.loads(f.readline())

    def test_requests_in_order(self):
        s, f = self.connect()
        with s:
            s.sendall(b'pos\r\nx-scene\nnope\n')
            replies = [json.loads(f.readline()) for _ in range(3)]
        self.assertEqual(replies[0]['data']['heading'], 316.3)
        self.assertEqual(replies[1]['data']['scene'], 'MainField')
        self.assertFalse(replies[2]['ok'])

    def test_http_closed(self):
        s, f = self.connect()
        with s:
            s.sendall(b'GET /pos HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n')
            self.assertEqual(f.readline(), b'')

    def test_long_line_closed(self):
        s, f = self.connect()
        with s:
            s.sendall(b'p' * (MAX_LINE + 10) + b'\n')
            self.assertEqual(f.readline(), b'')

    def test_connection_limit(self):
        held = [self.connect() for _ in range(MAX_CONNECTIONS)]
        for s, f in held:
            self.assertTrue(self.ask(s, f, b'state\n')['ok'])
        s, f = self.connect()
        with s:
            self.assertEqual(f.readline(), b'')   # the fifth is closed
        held[0][1].close()
        held[0][0].close()   # the reader holds the socket open too
        time.sleep(0.2)
        s, f = self.connect()
        with s:
            self.assertTrue(self.ask(s, f, b'state\n')['ok'])
        for s, _ in held[1:]:
            s.close()

    def test_port_taken(self):
        with self.assertRaises(OSError):
            BridgeServer(Bridge(FakeReader()), port=self.server.port)


if __name__ == '__main__':
    unittest.main()
