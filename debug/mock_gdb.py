"""Fake GdbSession with the same interface as gdb_bridge.GdbSession.

Lets the debug feature be exercised on a Lab Pi (or a dev machine) with no
OpenOCD, GDB toolchain, or hardware probe attached. Enabled by setting
DEBUG_MOCK=1 in the Lab Pi's environment (see app.py).

Ported from the standalone Debugger/ prototype (backend/app/mock_gdb.py),
swapping asyncio's `loop.call_later` for eventlet's `spawn_after` — same
"fire this after N seconds, cancellable" shape, just eventlet-native to
match the real GdbSession's port (see gdb_bridge.py's module docstring for
why).
"""

from __future__ import annotations

from typing import Any, Callable

import eventlet

MOCK_REGISTERS = {
    **{f"r{i}": f"0x{0:08x}" for i in range(13)},
    "sp": "0x20004ff0",
    "lr": "0xfffffff9",
    "pc": "0x08000188",
    "xpsr": "0x61000000",
}


class MockGdbSession:
    def __init__(self, board, gdb_port, on_event: Callable[[dict[str, Any]], None]):
        self._on_event = on_event
        self._pc = 0x08000188
        self._next_bp_id = 1
        self._symbols_loaded = False
        self._pending_continue = None
        self._watches: dict[str, str] = {}
        self._next_watch_id = 1

    def start(self) -> None:
        self._on_event({"event": "console", "text": "[mock] session started, no real hardware attached\n"})
        # Mirrors GdbSession.start(): reset to a known state on connect,
        # same as real hardware, so mock testing exercises the same flow.
        self.reset()

    def stop(self) -> None:
        if self._pending_continue is not None:
            self._pending_continue.cancel()

    def release_target(self) -> bool:
        # Mirrors GdbSession.release_target(): breakpoints gone, target
        # left running, and no further events pushed to the browser.
        if self._pending_continue is not None:
            self._pending_continue.cancel()
            self._pending_continue = None
        self._on_event = lambda payload: None
        return True

    def load_symbols(self, elf_path: str) -> None:
        self._on_event({"event": "console", "text": f"[mock] pretending to load symbols from {elf_path}\n"})
        self._symbols_loaded = True

    def cont(self) -> None:
        self._pc += 0x10
        self._pending_continue = eventlet.spawn_after(0.3, self._fire_continue_stop)

    def _fire_continue_stop(self) -> None:
        self._pending_continue = None
        self._push_stopped("breakpoint-hit")

    def step(self) -> None:
        # Mocks C-line vs. instruction stepping the same way the real
        # GdbSession does: bigger PC jump once "symbols" are loaded, to
        # make the mode difference visible while testing the UI.
        self._pc += 0x4 if self._symbols_loaded else 0x2
        self._push_stopped("end-stepping-range")

    def step_over(self) -> None:
        self._pc += 0x8 if self._symbols_loaded else 0x4
        self._push_stopped("end-stepping-range")

    def pause(self) -> None:
        if self._pending_continue is not None:
            self._pending_continue.cancel()
            self._pending_continue = None
        self._push_stopped("signal-received")

    def reset(self, halt: bool = True) -> None:
        if self._pending_continue is not None:
            self._pending_continue.cancel()
            self._pending_continue = None
        self._pc = 0x08000188
        self._on_event({"event": "console", "text": "[mock] monitor reset halt\n"})
        self._push_stopped("reset")

    def set_breakpoint(self, addr: str) -> dict[str, Any]:
        bp_id = self._next_bp_id
        self._next_bp_id += 1
        return {"payload": {"bkpt": {"number": str(bp_id), "addr": addr}}}

    def remove_breakpoint(self, bp_id: int) -> dict[str, Any]:
        return {"payload": {}}

    def read_registers(self) -> dict[str, str]:
        regs = dict(MOCK_REGISTERS)
        regs["pc"] = f"0x{self._pc:08x}"
        return regs

    def read_memory(self, addr: str, length: int) -> dict[str, Any]:
        pattern = "de ad be ef " * ((length // 4) + 1)
        return {"addr": addr, "bytes": pattern[: length * 3]}

    def disassemble(self, addr: str, count: int) -> list[dict[str, Any]]:
        base = self._pc
        return [
            {
                "address": f"0x{base + i * 2:08x}",
                "func-name": "main",
                "offset": str(i * 2),
                "inst": "movs r0, #0" if i % 2 == 0 else "bl 0x8000200 <delay>",
            }
            for i in range(count)
        ]

    def read_locals(self) -> list[dict[str, Any]]:
        if not self._symbols_loaded:
            return []
        return [
            {"name": "i", "type": "int", "value": str(self._pc % 10)},
            {"name": "ledState", "type": "int", "value": str((self._pc // 4) % 2)},
        ]

    def add_watch(self, expr: str) -> dict[str, Any]:
        name = f"mockwatch{self._next_watch_id}"
        self._next_watch_id += 1
        self._watches[name] = expr
        return {"name": name, "expr": expr, "type": "int", "value": str(self._pc % 100)}

    def remove_watch(self, name: str) -> None:
        self._watches.pop(name, None)

    def update_watches(self) -> list[dict[str, Any]]:
        return [
            {"name": name, "expr": expr, "value": str(self._pc % 100), "in_scope": "true"}
            for name, expr in self._watches.items()
        ]

    def _push_stopped(self, reason: str) -> None:
        self._on_event(
            {
                "event": "stopped",
                "reason": reason,
                "pc": f"0x{self._pc:08x}",
                "frame": {"addr": f"0x{self._pc:08x}", "func": "main"},
                "file": "main.c" if self._symbols_loaded else None,
                "line": (self._pc % 40) + 1 if self._symbols_loaded else None,
            }
        )
