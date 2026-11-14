"""python -m unittest discover -s tests (from the repository root). No game needed: a fake reader stands in."""
import json
import math
import socket
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from totk_live_bridge import PROTOCOL, __version__  # noqa: E402
from totk_live_bridge.protocol import Bridge, decode_flags  # noqa: E402
from totk_live_bridge.reader import LiveError, UnsupportedBuild  # noqa: E402
from totk_live_bridge.server import MAX_CONNECTIONS, MAX_LINE, BridgeServer  # noqa: E402


def turn(deg):
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    return (c, 0.0, s, 0.0, 1.0, 0.0, -s, 0.0, c)


class FakeReader:
    def __init__(self, pos=(843.4, 119.1, 849.6), rot=turn(-136.3), scene='MainField', fail=None):
        self.pos, self.rot, self._scene, self.fail = pos, rot, scene, fail

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
