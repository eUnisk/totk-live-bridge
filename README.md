# totk-live-bridge

Live data from *The Legend of Zelda: Tears of the Kingdom* running in
[TotK Recomp](https://github.com/valentderah/totk-pc-recomp), for companion apps such as progress maps.

It reads the running game's memory, **read-only**, and answers on a local socket in the protocol of TotK Recomp's own
control socket (`docs/control.md` in [TotK Recomp PR #1148](https://github.com/valentderah/totk-pc-recomp/pull/1148)).
It answers the same `help`, `pos` and `state`, and adds `x-` commands for what that socket doesn't answer (yet):
the progress flags, the inventory and the shrine Link is in.

**A stopgap.** Reading another program's memory breaks with every game or recomp build, and antivirus software may
find it suspicious. An app written against the protocol uses the official socket for whatever it answers and this
bridge only for the rest, so it keeps working as the official socket grows.

Supported: **TotK 1.4.3**, on **Windows** (tested in TotK Recomp 0.4.0 and 0.4.1). The addresses it reads are the game's own,
so they follow the game version; a newer TotK Recomp that loads the game the same way keeps them. Either way the bridge
checks the game's data tables first: with any other build it reads nothing, and every data command answers
`ok: false` with the supported build named. (Linux, where TotK Recomp also runs, would need a `/proc/<pid>/mem`
reader; not done yet.)

## Use

```
pip install git+https://github.com/eUnisk/totk-live-bridge@v0.1.0
totk-live-bridge                 # listens on 127.0.0.1:7778
totk-live-bridge --once pos      # one answer, no socket
```

Or in a Python app: `from totk_live_bridge import BridgeServer; BridgeServer(port=7778).start()`.

It never writes to the game's memory or files: the process is opened with `PROCESS_VM_READ` and
`PROCESS_QUERY_LIMITED_INFORMATION` only.

## Protocol

As the official socket: TCP on `127.0.0.1` only; one request per line, one JSON reply per line, in order,
`{"ok":true,"data":{...}}` or `{"ok":false,"error":"..."}`; a connection whose first line looks like an HTTP request
is closed (so web pages can't read the game); at most 4 connections; a line over 512 bytes closes the connection.
Port 7778 by default, next to the official 7777, so both can run.

| command | answers |
|---|---|
| `help` | `commands` (`name`, `args`, `help`), and `bridge`: `name`, `version`, `protocol`, `supported`, `available`, `build_ok` (true / false / null: can't tell yet), `error` |
| `pos` | `x`, `y`, `z`, `yaw`, `heading`, `place`, as the official `pos`; `No save loaded` on the title screen. Inside a shrine: the shrine's own coordinates (see `x-scene`) |
| `state` | `game`, `uptime_s`, `world_s`, `in_world` (the official fields the bridge can know). `world_s`: game time since the save loaded, left out before; TotK Recomp's own clock, read from the `totk.exe` it runs (its symbol table names it), so it also follows the recomp build |
| `x-scene` | `scene`: `MainField` (sky, surface and Depths) or the shrine, e.g. `Dungeon062`; `shrine`: true in one |
| `x-flags` | the GameData flags and the set GUIDs, as in `progress.sav`: `flags` = base64 of little-endian `u32 hash, u32 value` pairs, `guids` = base64 of `u64`s, with `count` and `guid_count` |
| `x-flags-since <since> [cursor]` | what changed in `x-flags` after scan `since`, a page (1000 entries) at a time: `flags` (as in `x-flags`), `guids_on`, `guids_off` (base64 of `u64`s), with `seq`, `base`, `reset`, `more`, `cursor`, `count`. The game is read again when the copy is over 0.2 s old |
| `x-inventory` | `armor`: the armor actors carried; `materials`: `{actor: count}` |

`x-flags-since` saves reading 325 KB a second: a client asks with `since` 0 and the `cursor` of each reply until
`more` is false (the same `since` on every page), applies the pages, and asks next time with the first page's `seq`.
`reset` true (on every page of that answer) means the whole state follows: drop what you had on the page with
`cursor` 0. Should `base` differ between pages, start over from `since` 0. `seq` counts from the time in ms, so a
`since` kept from before the bridge restarted gets a reset. A command without `args` takes none (`Usage: pos`), as
on the official socket.

The official socket has no `bridge` in its `help`: that is how a client tells the two apart. A client should check
`bridge.protocol` (bumped when a reply changes in a way an existing client can't read) and `bridge.build_ok`
before it trusts any data.

## Tests

`python -m unittest discover -s tests` (no game needed).

## AI use

This project was written with an AI assistant, under my supervision: I directed the work, reviewed it, and checked
it against the running game.

- Model: Claude Opus 5.5 (`claude-opus-5-5`) by Anthropic
- Tool: Claude Code, in the Claude desktop app for Windows

## License

MIT. Not affiliated with Nintendo or with TotK Recomp.

---

## 中文说明

给进度地图这类辅助工具用的实时数据桥：只读方式读取 TotK Recomp 中运行的游戏内存，在本机 7778 端口按 TotK Recomp
官方控制接口（PR #1148 的 `docs/control.md`）的协议应答。`help`、`pos`、`state` 与官方一致，另外用 `x-` 开头的命令提供
官方接口暂时没有的数据：进度 flag、背包、所在神庙。

这是过渡方案：游戏或 recomp 一更新，内存地址就可能失效。bridge 启动后先校验游戏数据表，版本不符时不读任何数据，
所有数据命令都返回 `ok: false` 并注明支持的版本。目前支持 Windows 上的 TotK 1.4.3（在 TotK Recomp 0.4.0 和 0.4.1 上测试）：
读取的地址属于游戏本身，跟随游戏版本；recomp 升级后只要游戏的载入方式不变就仍然可用，校验会自动判断。
