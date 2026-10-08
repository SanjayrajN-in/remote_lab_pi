"""Translates JSON commands from the browser (relayed through the Master —
see app.py's `debug_command` handler) into GdbSession calls, and normalizes
GdbSession replies into the `debug_event` shapes the frontend expects.

Client -> server commands: continue, step, step_over, pause, reset,
set_breakpoint, remove_breakpoint, read_memory, read_registers, disassemble,
read_locals, add_watch, remove_watch, update_watches.

Server -> client events: stopped, registers, memory, disasm, breakpoint,
locals, watch, watch_update, console, error.

Ported from the standalone Debugger/ prototype (backend/app/ws_protocol.py).
`handle_command` was `async def` there (GdbSession's methods were themselves
async); the whole file is now a plain synchronous function to match the
eventlet-native port of GdbSession (see gdb_bridge.py) — behavior is
otherwise unchanged.
"""

from __future__ import annotations

from typing import Any, Protocol


class GdbLike(Protocol):
    def cont(self) -> None: ...
    def step(self) -> None: ...
    def step_over(self) -> None: ...
    def pause(self) -> None: ...
    def reset(self, halt: bool = True) -> None: ...
    def set_breakpoint(self, addr: str) -> dict[str, Any]: ...
    def remove_breakpoint(self, bp_id: int) -> dict[str, Any]: ...
    def read_registers(self) -> dict[str, str]: ...
    def read_memory(self, addr: str, length: int) -> dict[str, Any]: ...
    def disassemble(self, addr: str, count: int) -> list[dict[str, Any]]: ...
    def read_locals(self) -> list[dict[str, Any]]: ...
    def add_watch(self, expr: str) -> dict[str, Any]: ...
    def remove_watch(self, name: str) -> None: ...
    def update_watches(self) -> list[dict[str, Any]]: ...


# Commands that only trigger a later async "stopped" event via on_event.
_FIRE_AND_FORGET = {"continue", "step", "step_over", "pause", "reset"}


class BadArgument(ValueError):
    """Client sent a missing/invalid field; reported as an 'error' event."""


def _int_arg(msg: dict[str, Any], key: str, default: int | None = None, minimum: int = 1) -> int:
    value = msg.get(key, default)
    # bool is an int subclass -- true/false is never a meaningful id/length.
    if isinstance(value, bool) or value is None:
        raise BadArgument(f"{key} must be a number, got {value!r}")
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if not isinstance(value, int) or value < minimum:
        raise BadArgument(f"{key} must be a whole number >= {minimum}, got {value!r}")
    return value


def _str_arg(msg: dict[str, Any], key: str) -> str:
    value = msg.get(key)
    if not isinstance(value, str) or not value.strip():
        raise BadArgument(f"{key} is required, got {value!r}")
    # These are interpolated into one GDB/MI command line; a newline would
    # let a client append a second, arbitrary GDB command.
    if any(ord(c) < 0x20 or ord(c) == 0x7f for c in value):
        raise BadArgument(f"{key} contains control characters")
    return value


def _gdb_error(resp: dict[str, Any]) -> str | None:
    """GDB's error text if this MI reply is an ^error, else None."""
    if resp.get("message") == "error":
        return (resp.get("payload") or {}).get("msg") or "unknown GDB error"
    return None


def handle_command(session: GdbLike, msg: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(msg, dict):
        return {"event": "error", "message": f"debug_command must be an object, got {type(msg).__name__}"}
    try:
        return _handle(session, msg)
    except BadArgument as e:
        return {"event": "error", "message": f"{msg.get('cmd')}: {e}"}


def _handle(session: GdbLike, msg: dict[str, Any]) -> dict[str, Any] | None:
    cmd = msg.get("cmd")

    if cmd == "continue":
        session.cont()
        return None
    if cmd == "step":
        session.step()
        return None
    if cmd == "step_over":
        session.step_over()
        return None
    if cmd == "pause":
        session.pause()
        return None
    if cmd == "reset":
        session.reset()
        return None

    if cmd == "set_breakpoint":
        addr = _str_arg(msg, "addr")
        resp = session.set_breakpoint(addr)
        err = _gdb_error(resp)
        bkpt = (resp.get("payload") or {}).get("bkpt") or {}
        number = str(bkpt.get("number", ""))
        if err is None and not number.isdigit():
            err = "GDB did not return a breakpoint number"
        if err is not None:
            # Never a 'set' for a breakpoint that doesn't exist.
            return {"event": "error", "message": f"Could not set breakpoint at {addr}: {err}"}
        return {"event": "breakpoint", "action": "set", "id": int(number), "addr": bkpt.get("addr")}

    if cmd == "remove_breakpoint":
        bp_id = _int_arg(msg, "id")
        err = _gdb_error(session.remove_breakpoint(bp_id) or {})
        # "No breakpoint number N." means it's already gone -- same end
        # state the client asked for, so still confirm the removal.
        if err is not None and "No breakpoint number" not in err:
            return {"event": "error", "message": f"Could not remove breakpoint {bp_id}: {err}"}
        return {"event": "breakpoint", "action": "removed", "id": bp_id}

    if cmd == "read_registers":
        values = session.read_registers()
        return {"event": "registers", "values": values}

    if cmd == "read_memory":
        result = session.read_memory(_str_arg(msg, "addr"), _int_arg(msg, "length"))
        return {"event": "memory", **result}

    if cmd == "disassemble":
        lines = session.disassemble(_str_arg(msg, "addr"), _int_arg(msg, "count", default=20))
        return {"event": "disasm", "lines": lines}

    if cmd == "read_locals":
        variables = session.read_locals()
        return {"event": "locals", "variables": variables}

    if cmd == "add_watch":
        result = session.add_watch(_str_arg(msg, "expr"))
        if "error" in result:
            return {"event": "error", "message": result["error"]}
        return {"event": "watch", "action": "added", **result}

    if cmd == "remove_watch":
        name = _str_arg(msg, "name")
        session.remove_watch(name)
        return {"event": "watch", "action": "removed", "name": name}

    if cmd == "update_watches":
        changes = session.update_watches()
        return {"event": "watch_update", "changes": changes}

    return {"event": "error", "message": f"unknown command: {cmd!r}"}


def is_fire_and_forget(cmd: str) -> bool:
    return cmd in _FIRE_AND_FORGET
