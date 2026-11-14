"""Read Tears of the Kingdom's progress and Link's position live from the running TotK Recomp (totk.exe).

A stopgap: reading another program's memory breaks with every game or recomp build and can look like a cheat tool
to antivirus software. Apps don't use this module: they talk to the bridge's socket (totk_live_bridge.server), which
speaks the protocol of TotK Recomp's own control socket (its docs/control.md), the proper way for anything it answers.

Read-only: the process is opened with PROCESS_VM_READ | PROCESS_QUERY_LIMITED_INFORMATION and nothing is ever
written to it. Windows only; elsewhere (and when the game isn't running, or is a build these offsets weren't
found for) `LiveReader.status()['available']` is False and the app keeps reading the saves.

TotK Recomp runs the game with guest address == host address and loads `main` at 0x7000000000, so the
addresses below are the same every start. They were found for TotK 1.4.3 with TotK Recomp 0.4.0 and are
checked against the vtables of the GameData tables before anything is read.

GameData (the flags the game writes to progress.sav):
    [main+0x3ad66b0] -> root R
    R + 8 + 0x60*i: one table per data type
        +0x00 vtable  +0x08 count  +0x10 entries*  +0x18 hash map*  +0x20 (capacity << 32) | count
        hash map: `capacity` slots of {u32 hash, u32 entry index}, empty slots are 0
    value of an entry (first copy; the second is the value at the last save point):
        Bool  (table 0)  stride 24  u8  at +0x16
        Int   (table 2)  stride 40  s32 at +0x1C
        Float (table 4)  stride 40  f32 at +0x1C
        Enum  (table 6)  stride 56  u32 at +0x1C (murmur3 of the enum value's name)
        UInt  (table 20) stride 40  u32 at +0x1C
    array tables: an entry holds {u64 length, items*} twice; both follow the game (an apple eaten changes
        both before any save), the reader takes the first
        Int array      (table 3)   stride 0x40, arrays at +0x20  items: s32
        String64 array (table 17)  stride 0x90, arrays at +0x70  items: 0x50 bytes {char *text, u32 capacity,
                                                                 char text[64]}
    R + 0xc58: count, R + 0xc60: array of {u64 guid, u64 attributes} sorted by guid; attribute bit 32 set =
    the object's flag is on (opened chest, read sign, ...): the same set as the GUID array of progress.sav.

Link: a Vec3f world position followed by a 3x3 rotation, reached from several static pointers. They all
agree; the reader takes the position most of them give and the rotation from one that has it.

The world clock (state's world_s) is TotK Recomp's own, not the game's: `world_us` (s64, microseconds; -1 until the
first save has loaded) in totk.exe's .data, the sum of the frame steps since the first present that found Link away
from the origin. A later load (fast travel, another save, the title screen) doesn't restart it; the official socket
answers it as world_s. totk.exe is an ordinary relocated image, so its address follows the recomp build: the reader
finds it by name in the COFF symbol table of the totk.exe file the game runs from (TotK Recomp 0.4.0 ships one),
after checking that the file's header and build id match the image in memory. A build without the symbol (or a
changed file) only leaves world_s out.
"""
import ctypes
import math
import struct
import sys
import threading
import time

WINDOWS = sys.platform == 'win32'
GAME_EXE = 'totk.exe'
MAIN = 0x7000000000
GD_ROOT_PTR = MAIN + 0x3ad66b0
GUID_COUNT_OFF, GUID_ARRAY_OFF = 0xc58, 0xc60
TABLE_BASE, TABLE_SIZE = 8, 0x60
# table index: (name, entry stride, value offset, struct format)
SCALAR_TABLES = {
    0: ('Bool', 24, 0x16, 'B'),
    2: ('Int', 40, 0x1C, 'i'),
    4: ('Float', 40, 0x1C, 'f'),
    6: ('Enum', 56, 0x1C, 'I'),
    20: ('UInt', 40, 0x1C, 'I'),
}
# vtables of the tables above in this build: a different game or recomp build has other addresses
EXPECTED_VTABLES = {0: 0x70039403d0, 2: 0x7003940510, 3: 0x70039405b0, 4: 0x7003940650, 6: 0x7003940790,
                    17: 0x7003940e70, 20: 0x7003941050}
# array tables: index -> (name, entry stride, offset of the current {length, items*}, item size)
INT_ARRAY, STRING64_ARRAY = 3, 17
ARRAY_TABLES = {INT_ARRAY: ('Int array', 0x40, 0x20, 4), STRING64_ARRAY: ('String64 array', 0x90, 0x70, 0x50)}
STRING64_TEXT = 12      # where the text of a String64 item starts
MAX_ARRAY = 4096
# the inventory arrays (murmur3 of Pouch.Armor.Content.Name, Pouch.Material.Content.Name / .StockNum)
ARMOR_NAMES, MATERIAL_NAMES, MATERIAL_COUNTS = 0x754e8549, 0xd96ebf12, 0xde2d8500
GUID_SET_BIT = 1 << 32
PLAYTIME_HASH = 0xE573F564
# (static offset in main, offsets added after each dereference) -> Vec3f position, then 3x3 rotation
PLAYER_CHAINS = [
    (0x3a4f4c0, (0x338,)),
    (0x3a4fc48, (0xf0,)),
    (0x3ad8b28, (0xa0,)),
    (0x3a5e9a0, (0x150,)),
    (0x3ad7528, (0x330, 0x2b4)),
]
# Where Link is: the object [main+SCENE_OBJ] (it also tracks his position for the VolumeStats; a stable pointer,
# like the chains above) has two area parts, the open world at +SCENE_WORLD and shrines at +SCENE_SHRINE, and at
# +SCENE_CUR a pointer to the one in use. The shrine part keeps the last shrine's name in a fixed string
# {char *text, u32 capacity, char text[]} at +SCENE_STR, also after Link has left it: only the pointer says
# whether he is in it. Inside a shrine his position is in that shrine's own coordinates, not the world map's.
SCENE_OBJ, SCENE_CUR, SCENE_WORLD, SCENE_SHRINE, SCENE_STR = 0x3a4fc48, 0x108, 0x110, 0x128, 0x140
WORLD_SCENE = 'MainField'   # what scene() calls the open world (sky, surface and Depths)
# The addresses are the game's own (its code and data at fixed guest addresses), so they follow the game version;
# a newer TotK Recomp that loads the game the same way keeps them. The vtable check decides either way.
WORLD_CLOCK = b'world_us'    # TotK Recomp's world clock, a static s64 in totk.exe's .data (see above)
MAX_WORLD_US = 10 * 365 * 86400 * 10**6     # anything above is not a clock
GAME_VERSION, RECOMP_VERSION = '1.4.3', '0.4.0'   # the game they were found for, and the recomp it was tested in
SUPPORTED = f'TotK {GAME_VERSION} (tested in TotK Recomp {RECOMP_VERSION})'


class LiveError(Exception):
    pass


class UnsupportedBuild(LiveError):
    """The game runs, but it is not the build these addresses were found in."""


def pe_static(f, name):
    """Where the static variable `name` (bytes) of a PE file is, from its COFF symbol table:
    (rva, file header, build id) or None when the file has no such symbol in a data section. file header: the 20
    bytes after 'PE\\0\\0', which the loader leaves as they are; build id: (rva, bytes) of the .buildid section, None
    without one. Together they tell whether the file is the image running."""
    head = f.read(0x1000)
    if head[:2] != b'MZ':
        return None
    pe = struct.unpack_from('<I', head, 0x3c)[0]
    if head[pe:pe + 4] != b'PE\0\0':
        return None
    fh = head[pe + 4:pe + 24]
    nsec, _, symptr, nsyms, optsz = struct.unpack_from('<HIIIH', fh, 2)
    sections = []
    for i in range(nsec):
        o = pe + 24 + optsz + 40 * i
        if o + 40 > len(head):
            return None
        sname = head[o:o + 8].rstrip(b'\0')
        vsize, va, rsize, raw = struct.unpack_from('<IIII', head, o + 8)
        sections.append((sname, va, vsize, raw, rsize))
    if not symptr or not 0 < nsyms < 10**7:
        return None
    f.seek(symptr)
    table = f.read(18 * nsyms + 4)
    if len(table) != 18 * nsyms + 4:
        return None
    strsize = struct.unpack_from('<I', table, 18 * nsyms)[0]
    if len(name) <= 8:
        keys = [name.ljust(8, b'\0')]
    else:   # a long name is {0, offset into the string table (which counts its own 4-byte size)}
        strings = b'\0' + f.read(max(0, strsize - 4))
        keys, at = [], strings.find(b'\0' + name + b'\0')
        while at >= 0:
            keys.append(struct.pack('<II', 0, at + 4))
            at = strings.find(b'\0' + name + b'\0', at + 1)
    for key in keys:
        at = table.find(key)
        while 0 <= at < 18 * nsyms:
            if at % 18 == 0:
                value, sec, _, cls = struct.unpack_from('<IhHB', table, at + 8)
                if cls in (2, 3) and 0 < sec <= nsec and sections[sec - 1][0] in (b'.data', b'.bss'):
                    if value + 8 <= sections[sec - 1][2]:
                        buildid = None
                        for sname, va, vsize, raw, rsize in sections:
                            if sname == b'.buildid' and 0 < vsize <= min(rsize, 256):
                                f.seek(raw)
                                buildid = (va, f.read(vsize))
                        return sections[sec - 1][1] + value, fh, buildid
            at = table.find(key, at + 1)
    return None


if WINDOWS:
    from ctypes import wintypes as wt

    _k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    _k32.OpenProcess.restype = wt.HANDLE
    _k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    _k32.ReadProcessMemory.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
                                       ctypes.POINTER(ctypes.c_size_t)]
    _k32.CloseHandle.argtypes = [wt.HANDLE]
    _k32.GetExitCodeProcess.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
    _k32.CreateToolhelp32Snapshot.restype = wt.HANDLE
    PROCESS_VM_READ = 0x0010
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    TH32CS_SNAPPROCESS = 0x2

    class _PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [('dwSize', wt.DWORD), ('cntUsage', wt.DWORD), ('th32ProcessID', wt.DWORD),
                    ('th32DefaultHeapID', ctypes.c_size_t), ('th32ModuleID', wt.DWORD), ('cntThreads', wt.DWORD),
                    ('th32ParentProcessID', wt.DWORD), ('pcPriClassBase', ctypes.c_long), ('dwFlags', wt.DWORD),
                    ('szExeFile', ctypes.c_wchar * 260)]

    class _MODULEENTRY32W(ctypes.Structure):
        _fields_ = [('dwSize', wt.DWORD), ('th32ModuleID', wt.DWORD), ('th32ProcessID', wt.DWORD),
                    ('GlblcntUsage', wt.DWORD), ('ProccntUsage', wt.DWORD), ('modBaseAddr', ctypes.c_void_p),
                    ('modBaseSize', wt.DWORD), ('hModule', wt.HMODULE), ('szModule', ctypes.c_wchar * 256),
                    ('szExePath', ctypes.c_wchar * 260)]

    TH32CS_SNAPMODULE = 0x8
    _k32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
    _k32.Process32FirstW.argtypes = _k32.Process32NextW.argtypes = [wt.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    _k32.Module32FirstW.argtypes = [wt.HANDLE, ctypes.POINTER(_MODULEENTRY32W)]

    def main_module(pid):
        """(load address, path) of the process's exe, or None. Needs no process handle."""
        for _ in range(3):   # the snapshot can fail while the process loads a module (ERROR_BAD_LENGTH)
            snap = _k32.CreateToolhelp32Snapshot(TH32CS_SNAPMODULE, pid)
            if snap and snap != wt.HANDLE(-1).value:
                break
        else:
            return None
        try:
            e = _MODULEENTRY32W()
            e.dwSize = ctypes.sizeof(e)
            if not _k32.Module32FirstW(snap, ctypes.byref(e)) or not e.modBaseAddr:
                return None
            return e.modBaseAddr, e.szExePath
        finally:
            _k32.CloseHandle(snap)

    def find_pid(name=GAME_EXE):
        snap = _k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if not snap or snap == wt.HANDLE(-1).value:
            return None
        try:
            e = _PROCESSENTRY32W()
            e.dwSize = ctypes.sizeof(e)
            ok = _k32.Process32FirstW(snap, ctypes.byref(e))
            while ok:
                if e.szExeFile.lower() == name:
                    return e.th32ProcessID
                ok = _k32.Process32NextW(snap, ctypes.byref(e))
        finally:
            _k32.CloseHandle(snap)
        return None


class _Process:
    """A read-only handle on the game process."""

    def __init__(self, pid):
        self.pid = pid
        self.h = _k32.OpenProcess(PROCESS_VM_READ | PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not self.h:
            raise LiveError(f'cannot open {GAME_EXE} (pid {pid}): error {ctypes.get_last_error()}')

    def alive(self):
        code = wt.DWORD()
        return bool(_k32.GetExitCodeProcess(self.h, ctypes.byref(code))) and code.value == STILL_ACTIVE

    def close(self):
        if self.h:
            _k32.CloseHandle(self.h)
            self.h = None

    def read(self, addr, size):
        if not addr or size <= 0:
            raise LiveError(f'bad read {addr:#x}+{size:#x}')
        buf = ctypes.create_string_buffer(size)
        n = ctypes.c_size_t()
        if not _k32.ReadProcessMemory(self.h, ctypes.c_void_p(addr), buf, size, ctypes.byref(n)) or n.value != size:
            raise LiveError(f'cannot read {size:#x} bytes at {addr:#x}')
        return buf.raw

    def u64(self, addr):
        return struct.unpack('<Q', self.read(addr, 8))[0]


class LiveReader:
    """Thread-safe; every call re-checks the process and returns None / raises LiveError when it can't read."""

    def __init__(self):
        self.lock = threading.Lock()
        self.proc = None
        self.error = None if WINDOWS else 'live reading needs Windows'
        self.maps = {}          # table index -> (key, {hash: entry index})
        self.next_try = 0.0     # don't look for the process more than once a second while it's not there
        self.clock = None       # (pid, address of the world clock or None when this build has none)

    # ---- process
    def _attach(self):
        if not WINDOWS:
            raise LiveError(self.error)
        if self.proc and self.proc.alive():
            return self.proc
        if self.proc:
            self.proc.close()
            self.proc = None
            self.maps.clear()
        now = time.monotonic()
        if now < self.next_try:
            raise LiveError(self.error or f'{GAME_EXE} is not running')
        self.next_try = now + 1.0
        pid = find_pid()
        if not pid:
            raise LiveError(f'{GAME_EXE} is not running')
        self.proc = _Process(pid)
        return self.proc

    def _root(self, p):
        root = p.u64(GD_ROOT_PTR)
        if not root:
            raise LiveError('game data not loaded yet')
        for ti, vt in EXPECTED_VTABLES.items():
            got = p.u64(root + TABLE_BASE + TABLE_SIZE * ti)
            if got != vt:
                raise UnsupportedBuild(f'unsupported game build (table {ti} vtable {got:#x}); live reading is made for {SUPPORTED}')
        return root

    def _call(self, fn):
        with self.lock:
            try:
                p = self._attach()
                result = fn(p)
                self.error = None
                return result
            except LiveError as e:
                self.error = str(e)
                raise
            except (OSError, struct.error, ValueError) as e:
                self.error = f'{type(e).__name__}: {e}'
                raise LiveError(self.error) from e

    # ---- game data
    def _index(self, p, root, ti, name):
        """(entries*, entry count, {hash: entry index}) of table ti; the hash map is read again only when it moved."""
        cnt, ent, hm, capcnt = struct.unpack('<4Q', p.read(root + TABLE_BASE + TABLE_SIZE * ti + 8, 32))
        cap = capcnt >> 32
        if not (0 < cnt < 200000 and cnt <= cap < 400000):
            raise LiveError(f'{name} table looks wrong (count {cnt}, capacity {cap})')
        key = (ent, hm, cnt, cap)
        cached = self.maps.get(ti)
        if not cached or cached[0] != key:
            raw = p.read(hm, cap * 8)
            index = {}
            for h, idx in struct.iter_unpack('<II', raw):
                if (h or idx) and idx < cnt:
                    index[h] = idx
            self.maps[ti] = cached = (key, index)
        return ent, cnt, cached[1]

    def _table(self, p, root, ti):
        name, stride, voff, fmt = SCALAR_TABLES[ti]
        ent, cnt, index = self._index(p, root, ti, name)
        entries = p.read(ent, stride * cnt)
        out = {}
        unpack = struct.Struct('<' + fmt).unpack_from
        for h, idx in index.items():
            v = unpack(entries, stride * idx + voff)[0]
            if fmt == 'f':
                v = struct.unpack('<I', struct.pack('<f', v))[0]   # as in the save: the float's bits
            out[h] = v & 0xFFFFFFFF
        return out

    def snapshot(self):
        """({hash: u32 value}, {guid}) in the shapes savefile.read_table / read_guids give for a save file."""
        def read(p):
            root = self._root(p)
            table = {}
            for ti in SCALAR_TABLES:
                for h, v in self._table(p, root, ti).items():
                    table.setdefault(h, v)
            if PLAYTIME_HASH not in table:
                raise LiveError('play time flag missing: unexpected game data layout')
            n = p.u64(root + GUID_COUNT_OFF)
            if not 0 < n < 2000000:
                raise LiveError(f'GUID table looks wrong (count {n})')
            raw = p.read(p.u64(root + GUID_ARRAY_OFF), 16 * n)
            guids = {g for g, a in struct.iter_unpack('<QQ', raw) if a & GUID_SET_BIT}
            return table, guids
        return self._call(read)

    def _array(self, p, root, ti, h):
        """The current items of array h in table ti: ints, or the texts of String64 items."""
        name, stride, aoff, size = ARRAY_TABLES[ti]
        ent, _, index = self._index(p, root, ti, name)
        if h not in index:
            raise LiveError(f'array {h:#010x} missing from the {name} table')
        n, items = struct.unpack('<QQ', p.read(ent + stride * index[h] + aoff, 16))
        if not 0 < n <= MAX_ARRAY:
            raise LiveError(f'array {h:#010x} looks wrong (length {n})')
        raw = p.read(items, size * n)
        if ti == INT_ARRAY:
            return list(struct.unpack(f'<{n}i', raw))
        out = []
        for i in range(n):
            text = raw[size * i + STRING64_TEXT:size * (i + 1)].split(b'\0', 1)[0]
            out.append(text.decode('utf-8', 'replace'))
        return out

    def inventory(self):
        """(set of armor actors carried, {material actor: count}) as savefile.read_inventory gives for a save."""
        def read(p):
            root = self._root(p)
            carried = {a for a in self._array(p, root, STRING64_ARRAY, ARMOR_NAMES) if a}
            names = self._array(p, root, STRING64_ARRAY, MATERIAL_NAMES)
            counts = self._array(p, root, INT_ARRAY, MATERIAL_COUNTS)
            mats = {}
            for n, c in zip(names, counts):
                if n and c > 0:
                    mats[n] = mats.get(n, 0) + c
            return carried, mats
        return self._call(read)

    # ---- Link
    def player(self):
        """((x, y, z), rotation): Link's world position and the 3x3 rotation stored after it as 9 floats in memory
        order, or None when no chain gave one that turns about y. protocol.yaw_from_rotation turns it into a yaw."""
        def read(p):
            got = []
            for st, offs in PLAYER_CHAINS:
                try:
                    a = MAIN + st
                    for d in offs:
                        a = p.u64(a) + d
                    f = struct.unpack('<12f', p.read(a, 48))
                except LiveError:
                    continue
                if all(math.isfinite(v) and abs(v) < 20000 for v in f[:3]):
                    got.append(f)
            if not got:
                raise LiveError("Link's position not found")
            # the position most chains agree on
            best = max(got, key=lambda f: sum(1 for g in got if abs(g[0] - f[0]) + abs(g[1] - f[1]) + abs(g[2] - f[2]) < 1))
            rot = None
            for f in got:   # a turn about y: the middle element is 1 and the first row a unit vector in x/z
                if abs(f[3] * f[3] + f[5] * f[5] - 1) < 0.05 and abs(f[7] - 1) < 0.05 and abs(f[0] - best[0]) < 1:
                    rot = tuple(f[3:12])
                    break
            return tuple(best[:3]), rot
        return self._call(read)

    def scene(self):
        """Where Link is: WORLD_SCENE in the open world, the shrine's name ('Dungeon062') in a shrine, or None when
        it doesn't read as either."""
        def read(p):
            obj = p.u64(MAIN + SCENE_OBJ)
            if not obj:
                return None
            cur = p.u64(obj + SCENE_CUR)
            if cur == obj + SCENE_WORLD:
                return WORLD_SCENE
            if cur != obj + SCENE_SHRINE:
                return None
            text, cap = struct.unpack('<QI', p.read(obj + SCENE_STR, 12))
            if text != obj + SCENE_STR + 12 or not 0 < cap <= 256:   # the text sits right after its header
                return None
            raw = p.read(text, cap).split(b'\0', 1)[0]
            return raw.decode('ascii') if raw and all(0x20 < c < 0x7f for c in raw) else None
        return self._call(read)

    # ---- TotK Recomp
    def _find_clock(self, p):
        """The address of the world clock in this process, or None (looked up once per process)."""
        if self.clock and self.clock[0] == p.pid:
            return self.clock[1]
        addr = None
        mod = main_module(p.pid)
        if mod:
            base, path = mod
            try:
                with open(path, 'rb') as f:
                    found = pe_static(f, WORLD_CLOCK)
            except OSError:
                found = None
            if found:
                rva, fh, buildid = found
                pe = struct.unpack('<I', p.read(base + 0x3c, 4))[0]
                same = p.read(base + pe + 4, 20) == fh   # the file is the image running, not a newer copy
                if same and buildid:
                    same = p.read(base + buildid[0], len(buildid[1])) == buildid[1]
                if same:
                    addr = base + rva
        self.clock = (p.pid, addr)
        return addr

    def world_us(self):
        """TotK Recomp's world clock: microseconds of game time since the first save loaded, or None before that or
        when this build has no clock the reader can find."""
        def read(p):
            addr = self._find_clock(p)
            if addr is None:
                return None
            w = struct.unpack('<q', p.read(addr, 8))[0]
            return w if 0 <= w < MAX_WORLD_US else None
        return self._call(read)

    def status(self):
        """{'available': bool, 'pid', 'build_ok', 'error'}: tries to attach (at most once a second while the game is
        off). build_ok: True for the supported build, False for another one, None when it couldn't tell (the game
        isn't running or hasn't loaded its data yet)."""
        try:
            self._call(self._root)
            return {'available': True, 'pid': self.proc.pid, 'build_ok': True, 'error': None}
        except LiveError as e:
            return {'available': False, 'pid': self.proc.pid if self.proc else None,
                    'build_ok': False if isinstance(e, UnsupportedBuild) else None, 'error': str(e)}


if __name__ == '__main__':   # quick check: python -m totk_live_bridge.reader
    r = LiveReader()
    print(r.status())
    t0 = time.perf_counter()
    table, guids = r.snapshot()
    print(f'{len(table)} flags, {len(guids)} GUIDs set, play time {table.get(PLAYTIME_HASH)} s '
          f'({(time.perf_counter() - t0) * 1000:.0f} ms)')
    carried, mats = r.inventory()
    print(f'inventory: {len(carried)} armor pieces, {len(mats)} materials')
    for _ in range(3):
        print(r.player())
        time.sleep(0.5)
