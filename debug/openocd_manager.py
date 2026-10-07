"""Lifecycle management for the OpenOCD subprocess.

Starts `openocd -f <interface.cfg> -f <target.cfg>`, waits for its GDB
server port to accept connections, and guarantees clean teardown (OpenOCD
holds the USB probe open, so a leaked process blocks the next session).

Ported from the standalone Debugger/ prototype (backend/app/openocd_manager.py).
The only change is dropping asyncio (`start()` is now a plain blocking call,
not `async def`) — this runs inside a Flask-SocketIO/eventlet process, where
plain `time.sleep()` is already cooperative (eventlet monkey-patches it), so
there was nothing asyncio was buying here.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import threading
import time

from .boards import BoardProfile


class OpenOCDStartupError(RuntimeError):
    pass


class OpenOCDManager:
    def __init__(self, board: BoardProfile, gdb_port: int = 3333, telnet_port: int = 4444,
                 adapter_serial: str | None = None):
        self.board = board
        self.gdb_port = gdb_port
        self.telnet_port = telnet_port
        # Selects a specific probe by its USB serial number when a Lab Pi has
        # more than one debug-capable board attached at once -- otherwise
        # OpenOCD just grabs whichever matching adapter it finds first. Not
        # every adapter driver supports this (some HLA and JTAG drivers
        # accept it, most don't reject it either); if a probe doesn't honor
        # it, clear the admin's "Debug probe" field to fall back.
        self.adapter_serial = adapter_serial
        self._proc: subprocess.Popen | None = None

    def start(self, timeout: float = 10.0) -> None:
        if self._proc is not None:
            raise RuntimeError("OpenOCD already running")

        cmd = ["openocd", "-f", self.board.openocd_interface]
        if self.adapter_serial:
            cmd += ["-c", f"adapter serial {self.adapter_serial}"]
        if self.board.openocd_transport:
            # Must come after the interface cfg (which selects the adapter
            # driver) and before the target cfg (which sources swj-dp.tcl
            # and needs the transport already decided).
            cmd += ["-c", f"transport select {self.board.openocd_transport}"]
        cmd += [
            "-f", self.board.openocd_target,
            "-c", f"gdb port {self.gdb_port}",
            "-c", f"telnet port {self.telnet_port}",
        ]
        self._proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,  # own process group -> can kill cleanly
            text=True,
        )

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                output = self._proc.stdout.read() if self._proc.stdout else ""
                raise OpenOCDStartupError(
                    f"openocd exited early (code {self._proc.returncode}):\n{output}"
                )
            if _port_open("127.0.0.1", self.gdb_port):
                # stdout is a pipe nobody else reads: once its ~64 KiB buffer
                # fills (a long or chatty session), OpenOCD blocks on write
                # and the debug session stalls. Drain it in the background.
                threading.Thread(target=_drain, args=(self._proc.stdout,), daemon=True).start()
                return
            time.sleep(0.2)

        self.stop()
        raise OpenOCDStartupError(
            f"openocd did not open gdb port {self.gdb_port} within {timeout}s "
            "(check probe is connected and board cfg matches your chip)"
        )

    def stop(self) -> None:
        if self._proc is None:
            return
        try:
            os.killpg(os.getpgid(self._proc.pid), signal.SIGTERM)
            self._proc.wait(timeout=5)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(os.getpgid(self._proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
        finally:
            self._proc = None

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None


def _drain(stream) -> None:
    try:
        for _ in iter(stream.readline, ""):
            pass
    except (OSError, ValueError):
        pass  # pipe closed by stop()


def _port_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex((host, port)) == 0
