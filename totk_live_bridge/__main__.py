"""python -m totk_live_bridge [--port 7778] | --once COMMAND"""
import argparse
import json
import sys

from . import DEFAULT_PORT, __version__
from .protocol import Bridge
from .server import HOST, BridgeServer


def main(argv=None):
    ap = argparse.ArgumentParser(prog='totk-live-bridge', description='Live data from TotK Recomp for companion apps '
                                 '(read-only), in the protocol of its control socket.')
    ap.add_argument('--port', type=int, default=DEFAULT_PORT, help=f'port on {HOST} (default {DEFAULT_PORT})')
    ap.add_argument('--once', metavar='COMMAND', help='answer one command and exit, without a socket')
    ap.add_argument('--version', action='version', version=f'totk-live-bridge {__version__}')
    args = ap.parse_args(argv)
    if args.once:
        print(json.dumps(Bridge().handle(args.once), ensure_ascii=False))
        return 0
    try:
        server = BridgeServer(port=args.port)
    except OSError as e:
        print(f'cannot listen on {HOST}:{args.port}: {e}', file=sys.stderr)
        return 1
    print(f'totk-live-bridge {__version__}: listening on {HOST}:{server.port} (Ctrl+C to stop)', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
