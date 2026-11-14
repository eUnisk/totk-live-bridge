"""The bridge's socket: the rules of TotK Recomp's control socket (docs/control.md there).

- TCP on 127.0.0.1 only;
- one request per line (`\\n`, a `\\r` before it is ignored), one JSON reply per line, in order;
- a connection whose first line looks like an HTTP request is closed before anything runs (a web page on this PC can
  send requests to 127.0.0.1; this keeps it from reading the game);
- at most 4 connections; a line longer than 512 bytes closes the connection.
"""
import json
import re
import socket
import threading

from . import DEFAULT_PORT
from .protocol import Bridge

HOST = '127.0.0.1'
MAX_CONNECTIONS = 4
MAX_LINE = 512
HTTP_LINE = re.compile(rb'^[A-Z]{3,10} \S+ HTTP/\d')


class BridgeServer:
    def __init__(self, bridge=None, port=DEFAULT_PORT):
        self.bridge = bridge or Bridge()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):   # Windows: never share the port with another listener
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        self.sock.bind((HOST, port))   # OSError when the port is taken
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        self.conns = set()
        self.lock = threading.Lock()
        self.closed = False
        self.thread = None

    def start(self):
        """Serve in a background (daemon) thread; returns self."""
        self.thread = threading.Thread(target=self.serve_forever, name='totk-live-bridge', daemon=True)
        self.thread.start()
        return self

    def serve_forever(self):
        self.sock.settimeout(0.5)   # wakes up now and then, so Ctrl+C and close() are seen
        while not self.closed:
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            conn.settimeout(None)
            if not self.slots.acquire(blocking=False):
                conn.close()
                continue
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        with self.lock:
            self.conns.add(conn)
        try:
            f = conn.makefile('rb')
            first = True
            while True:
                line = f.readline(MAX_LINE + 1)
                if not line:
                    break
                if len(line) > MAX_LINE or not line.endswith(b'\n'):
                    break   # too long (or cut off at the close)
                if first and HTTP_LINE.match(line):
                    break
                first = False
                try:
                    text = line.decode('utf-8').rstrip('\r\n')
                except UnicodeDecodeError:
                    break
                reply = self.bridge.handle(text)
                conn.sendall(json.dumps(reply, separators=(',', ':')).encode('utf-8') + b'\n')
        except OSError:
            pass
        finally:
            with self.lock:
                self.conns.discard(conn)
            try:
                conn.close()
            except OSError:
                pass
            self.slots.release()

    def close(self):
        self.closed = True
        try:
            self.sock.close()
        except OSError:
            pass
        with self.lock:
            conns = list(self.conns)
        for c in conns:
            try:
                c.shutdown(socket.SHUT_RDWR)
                c.close()
            except OSError:
                pass
