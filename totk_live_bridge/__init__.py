"""totk-live-bridge: live data from Tears of the Kingdom running in TotK Recomp, for companion apps.

It reads the running game's memory (read-only) and answers on a local socket in the protocol of TotK Recomp's own
control socket (docs/control.md there): the same `help`, `pos` and `state`, plus `x-` commands for what that socket
doesn't answer (yet): the progress flags, the inventory and the loaded scene.

A stopgap until the game's own socket answers everything: an app written against the protocol keeps working when it
switches to the official socket for the commands that has.
"""
__version__ = '0.1.0'
PROTOCOL = 1            # bumped when a reply changes in a way an existing client can't read
DEFAULT_PORT = 7778     # next to the official socket's 7777, so both can run
OFFICIAL_PORT = 7777

from .protocol import Bridge  # noqa: E402
from .server import BridgeServer  # noqa: E402

__all__ = ['Bridge', 'BridgeServer', '__version__', 'PROTOCOL', 'DEFAULT_PORT', 'OFFICIAL_PORT']
