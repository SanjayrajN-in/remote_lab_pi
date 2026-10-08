"""Bridges GDB/MI to the rest of the Lab Pi's Flask-SocketIO app.

Commands that need a reply (read registers/memory, disassemble, set/remove
breakpoint) are tagged with a numeric MI token; a background greenthread
drains every GDB/MI response and either resolves the matching pending
eventlet Event (tokened) or forwards it as a `debug_event` SocketIO emit
(untokened async notifications such as `*stopped` from continue/step/pause).

Adapted from the standalone Debugger/ prototype (backend/app/gdb_bridge.py).
That version was built on asyncio (a Future per pending token, an
asyncio.Queue of outbound events, `loop.call_soon_threadsafe` to hand
results back from GDB's reader thread) because it ran inside FastAPI, which
hands you an asyncio loop for free. This app runs on Flask-SocketIO +
eventlet instead, and mixing a second, independent asyncio event loop into
an eventlet-monkey-patched process is a well-known source of subtle
deadlocks — eventlet's monkey-patched socket/select primitives assume
everything happens on its own greenlet hub, not a separate real OS thread
running asyncio's own selector loop.

None of that machinery was actually load-bearing here: every use of it was
just "block the caller until a reply with this token shows up". That's
exactly what eventlet's own `Event` primitive does, and the reader
greenthread emits events directly instead of queueing them for something
else to forward -- so the port below just swaps the asyncio primitives for
their eventlet-native equivalents 1:1 and drops the loop argument and the
separate outbound queue+drain-task entirely.

One thing that ISN'T cooperative, though: pygdbmi spawns GDB itself via a
plain `subprocess.Popen(..., stdout=PIPE)` and then reads it with its own
fcntl-non-blocking-fd + raw `select.select()` loop (see IoManager.py) --
its own hand-rolled scheduling, not delegated to whatever event loop the
caller happens to be running. Under eventlet.monkey_patch(), that
`subprocess.Popen` resolves to `eventlet.green.subprocess.Popen`, whose
`.stdout` is a greenified pipe: `select.select()` still correctly reports
it readable, but the following `.read()` on that greenified pipe then
blocks -- forever, no exception -- instead of returning pygdbmi's own
already-non-blocking-marked fd's available bytes. Every debug session hung
for the full REGISTER_TIMEOUT on its very first blocking command (the
mandatory reset+halt in start()) until this was traced down to that:
GdbController below is built against the *real*, pre-monkey-patch
subprocess module, so GDB's pipe stays a genuine OS pipe/pty that pygdbmi's
own read loop was written for.
"""

from __future__ import annotations

import itertools
import shutil
from typing import Any, Callable

import eventlet
from eventlet.event import Event

# pygdbmi.gdbcontroller does `import subprocess` at module scope; if this
# module were imported after eventlet.monkey_patch() (as it is here -- see
# app.py), that binds to eventlet.green.subprocess. Swap it for the
# unpatched module first so pygdbmi always spawns GDB with a plain
# subprocess.Popen -- see the docstring above for why the green one hangs.
import pygdbmi.gdbcontroller as _gdbcontroller_mod
_gdbcontroller_mod.subprocess = eventlet.patcher.original('subprocess')
from pygdbmi.gdbcontroller import GdbController

from .boards import BoardProfile

REGISTER_TIMEOUT = 5.0

# A board profile's gdb_binary is a preference, not a hard requirement --
# most Lab Pi images already have one of these from whatever toolchain was
# used to build/flash the firmware, even if not the exact name a profile
# asks for. Tried in this order after the profile's own choice.
_GDB_FALLBACKS = ["gdb-multiarch", "arm-none-eabi-gdb", "gdb"]


def _resolve_gdb_binary(preferred: str) -> str:
    candidates = [preferred] + [c for c in _GDB_FALLBACKS if c != preferred]
    for candidate in candidates:
        if shutil.which(candidate):
            return candidate
    raise RuntimeError(
        f"no usable GDB binary found on this Lab Pi (tried: {', '.join(candidates)}). "
        "Install one, e.g. `sudo apt install gdb-multiarch` or "
        "`sudo apt install gdb-arm-none-eabi` (Debian/Raspberry Pi OS)."
    )


class GdbCommandTimeout(RuntimeError):
    pass


class GdbProcessExited(RuntimeError):
    pass


class GdbSession:
    def __init__(self, board: BoardProfile, gdb_port: int, on_event: Callable[[dict[str, Any]], None],
                 on_exit: Callable[[int | None], None] | None = None):
        self._board = board
        self._gdb_port = gdb_port
        # Called (from the reader greenthread) with every event this session
        # wants pushed to the browser — e.g. `lambda payload: socketio.emit(
        # 'debug_event', payload)` in app.py. Broadcasting with no explicit
        # room is correct here: a Lab Pi only ever has the one active
        # booking's browser actually listening (see current_session_key).
        self._on_event = on_event
        # Called once (from the reader greenthread, with GDB's exit code) if
        # GDB dies on its own rather than via stop() -- so the owner can
        # release OpenOCD/the probe and clear its session state.
        self._on_exit = on_exit

        gdb_binary = _resolve_gdb_binary(board.gdb_binary)
        # -readnow: GDB 16's lazy DWARF indexer hits an internal error
        # (dwarf2/read.c set_lang assertion) on TI-compiler (CCS) .out files
        # and aborts on "Load Symbols"; expanding all symtabs up front
        # avoids that code path. -iex ... corefile no: if GDB does hit an
        # internal error anyway, don't drop a core file in our cwd.
        self._controller = GdbController(
            command=[gdb_binary, "--nx", "--quiet", "-readnow",
                     "-iex", "maint set internal-error corefile no",
                     "--interpreter=mi3"]
        )
        self._token_counter = itertools.count(1)
        self._pending: dict[int, Event] = {}
        self._register_names: list[str] = []
        # Flask-SocketIO gives every incoming 'debug_command' its own
        # greenthread, so e.g. the four auto-refresh commands the frontend
        # fires on every single 'stopped' event (see the 'stopped' case in
        # templates/index.html) can genuinely overlap in-flight, especially
        # while stepping repeatedly. GDB/MI itself is a single serial
        # command/response stream, and pygdbmi's write() waits for write-
        # readiness via the (eventlet-patched) `select` module -- two
        # greenthreads racing there trips eventlet's own multiple-writers-
        # on-one-fd guard ("Second simultaneous write on fileno N
        # detected"), surfaced to the student as a bogus debug_event error.
        # A plain mutex around each write serializes them without changing
        # any of the request/response matching above (still done by token).
        self._write_lock = eventlet.Semaphore()

        self._reader_greenlet = None
        self._stop_reader = False
        # Set by release_target(): the session is being torn down, so the
        # *stopped/console records its own interrupt/reset produce must not
        # reach the browser (a 'stopped' would make it fire read_registers
        # etc. at a session that's about to be gone).
        self._closing = False
        # Set while start()/reset() run: GDB's own halt-on-attach *stopped,
        # the interrupt's *stopped and OpenOCD's "halted due to debug-request
        # ... pc:" monitor output all duplicate the single 'stopped' (reason
        # "reset") that reset() emits itself once the PC is known.
        self._muted = False
        self._symbols_loaded = False
        # GDB's var-object names (e.g. "var2") aren't the expression text,
        # so track expr alongside each one for display / re-sending to the UI.
        self._watches: dict[str, str] = {}

    # ---- lifecycle -----------------------------------------------------

    def start(self) -> None:
        self._reader_greenlet = eventlet.spawn(self._read_loop)
        # Without this, GDB processes MI commands synchronously: once
        # -exec-continue is sent it blocks reading stdin until the target
        # stops on its own, so pause/reset/anything else sent meanwhile is
        # silently queued and never delivered.
        self._write("-gdb-set mi-async on", tokened=False)
        self._muted = True
        self._write(f"-target-select remote localhost:{self._gdb_port}", tokened=False)
        # A freshly attached session otherwise inherits whatever halt/run
        # state the CPU was left in by the previous session — students
        # would start stepping from an arbitrary leftover point instead of
        # the actual entry point of whatever is currently flashed. Reset
        # to a known-good starting state every time a session begins.
        self.reset(halt=True)

    def stop(self) -> None:
        self._stop_reader = True
        if self._reader_greenlet is not None:
            self._reader_greenlet.kill()
            self._reader_greenlet = None
        # Not pygdbmi's exit(): its terminate()+wait() is the unpatched
        # subprocess (see module docstring), so a GDB that ignores SIGTERM
        # would block the whole eventlet hub. Poll cooperatively, then KILL.
        proc = self._controller.gdb_process
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                for _ in range(30):
                    if proc.poll() is not None:
                        break
                    eventlet.sleep(0.1)
                else:
                    proc.kill()
                    proc.wait()
            except Exception:
                pass
        if proc is not None:
            for pipe in (proc.stdin, proc.stdout, proc.stderr):
                try:
                    if pipe is not None:
                        pipe.close()
                except Exception:
                    pass
        self._controller.gdb_process = None
        self._fail_pending("GDB session stopped")

    def release_target(self) -> bool:
        """Leave the CPU running before the session is stopped: the Tiva is
        powered over the debug cable, so a target left halted at a
        breakpoint stays frozen until someone power-cycles it. Every step is
        best-effort -- one failing never skips the rest, and the caller
        still stops GDB/OpenOCD afterwards. Returns True if the target was
        resumed (reset-run or a fallback)."""
        self._closing = True

        def attempt(name: str, fn: Callable[[], Any]) -> bool:
            try:
                fn()
                return True
            except Exception as e:
                print(f"[Debug] release_target: {name} failed: {e}")
                return False

        # Breakpoint and monitor commands are refused while the target runs,
        # so halt it first -- but only if it IS running: an -exec-interrupt
        # sent to an already-halted target still reaches OpenOCD as a Ctrl-C,
        # which it holds as a pending halt and applies the moment the core
        # runs again (i.e. right after the reset-run below), re-freezing it.
        if self._target_running() is not False:
            attempt("interrupt", lambda: self._write("-exec-interrupt", tokened=False))
        attempt("delete breakpoints", lambda: self._console_retrying("delete"))
        resumed = (
            attempt(self._board.resume_monitor_cmd,
                    lambda: self._console_retrying(f"monitor {self._board.resume_monitor_cmd}"))
            or attempt("continue", lambda: self._ok_or_raise(self._send_and_wait("-exec-continue", timeout=2.0)))
            or attempt("monitor resume", lambda: self._console_retrying("monitor resume"))
        )
        attempt("detach", lambda: self._ok_or_raise(self._send_and_wait("-target-detach", timeout=2.0)))
        return resumed

    def load_symbols(self, elf_path: str) -> None:
        self._write(f"-file-exec-and-symbols {elf_path}", tokened=False)
        self._symbols_loaded = True

    # ---- fire-and-forget execution control ------------------------------

    def cont(self) -> None:
        self._write("-exec-continue", tokened=False)

    def step(self) -> None:
        # Source-line stepping needs function/line bounds from symbols;
        # without them GDB errors "Cannot find bounds of current
        # function", so fall back to instruction-level until an ELF is
        # loaded via load_symbols().
        cmd = "-exec-step" if self._symbols_loaded else "-exec-step-instruction"
        self._write(cmd, tokened=False)

    def step_over(self) -> None:
        cmd = "-exec-next" if self._symbols_loaded else "-exec-next-instruction"
        self._write(cmd, tokened=False)

    def pause(self) -> None:
        # Only if running: interrupting an already-halted target arms a
        # stale halt in OpenOCD (see release_target) that freezes the next
        # Continue straight away.
        if self._target_running() is not False:
            self._write("-exec-interrupt", tokened=False)

    def reset(self, halt: bool = True) -> None:
        # GDB refuses to run "monitor" commands while the target is
        # executing ("Cannot execute this command while the selected
        # thread is running") — it doesn't queue them, it just errors and
        # does nothing. So if we're mid -exec-continue, interrupt first.
        # A fixed sleep to wait for the halt to land is unreliable (timed
        # out in testing under real hardware jitter) — retry the monitor
        # command instead until it stops erroring, which adapts to
        # however long the actual halt takes.
        # Only interrupt a running target, though: GDB halts it on attach,
        # and an -exec-interrupt sent to an already-halted target still
        # reaches OpenOCD as a Ctrl-C it holds as a pending halt -- which made
        # the student's first Continue after Connect stop again immediately
        # ("signal-received").
        self._muted = True
        try:
            self._reset_target(halt)
        finally:
            self._muted = False
        if halt:
            pc = self.read_registers().get("pc", "")
            self._on_event({"event": "stopped", "reason": "reset", "pc": pc, "frame": {"addr": pc}, "file": None, "line": None})

    def _reset_target(self, halt: bool) -> None:
        if self._target_running() is not False:
            self._write("-exec-interrupt", tokened=False)
        cmd = "reset halt" if halt else "reset"
        monitor_cmd = f'-interpreter-exec console "monitor {cmd}"'
        for _ in range(15):
            resp = self._send_and_wait(monitor_cmd)
            if resp.get("message") != "error":
                break
            eventlet.sleep(0.2)

        if halt:
            # OpenOCD's reset+halt happens out-of-band from GDB's own
            # execution tracking (it went out over the monitor/qRcmd
            # channel, not GDB's normal exec control), so GDB never emits
            # its own *stopped record for it, and its register cache goes
            # stale (it'll happily return the pre-reset PC otherwise).
            # "maintenance flush register-cache" forces GDB to re-read
            # every register from the target instead of trusting its cache.
            self._send_and_wait('-interpreter-exec console "maintenance flush register-cache"')

    # ---- request/response commands --------------------------------------

    def set_breakpoint(self, addr: str) -> dict[str, Any]:
        return self._send_and_wait(f"-break-insert *{addr}")

    def remove_breakpoint(self, bp_id: int) -> dict[str, Any]:
        return self._send_and_wait(f"-break-delete {bp_id}")

    def read_registers(self) -> dict[str, str]:
        if not self._register_names:
            names_resp = self._send_and_wait("-data-list-register-names")
            self._register_names = names_resp.get("payload", {}).get("register-names", [])

        values_resp = self._send_and_wait("-data-list-register-values x")
        values = values_resp.get("payload", {}).get("register-values", [])
        result: dict[str, str] = {}
        for entry in values:
            idx = int(entry["number"])
            name = self._register_names[idx] if idx < len(self._register_names) else f"r{idx}"
            result[name] = entry["value"]
        return result

    def read_memory(self, addr: str, length: int) -> dict[str, Any]:
        resp = self._send_and_wait(f"-data-read-memory-bytes {addr} {length}")
        blocks = resp.get("payload", {}).get("memory", [])
        if not blocks:
            return {"addr": addr, "bytes": ""}
        return {"addr": blocks[0]["begin"], "bytes": blocks[0]["contents"]}

    def disassemble(self, addr: str, count: int) -> list[dict[str, Any]]:
        # Rough byte span for `count` Thumb/ARM instructions (up to 4 bytes each);
        # good enough for a debug view, not used for anything safety-critical.
        end = f"{addr}+{count * 4}"
        resp = self._send_and_wait(f"-data-disassemble -s {addr} -e {end} -- 0")
        return resp.get("payload", {}).get("asm_insns", [])

    def read_locals(self) -> list[dict[str, Any]]:
        # Needs symbols (load_symbols) and a stopped frame with debug info;
        # GDB errors otherwise ("No frame selected" / no symbol table) —
        # that just means "no locals to show", not a real failure.
        resp = self._send_and_wait("-stack-list-variables --simple-values")
        if resp.get("message") == "error":
            return []
        return resp.get("payload", {}).get("variables", [])

    def add_watch(self, expr: str) -> dict[str, Any]:
        # Escape so an expression containing a literal quote (e.g. a string
        # comparison) can't break out of the MI command's quoted argument.
        escaped = expr.replace("\\", "\\\\").replace('"', '\\"')
        resp = self._send_and_wait(f'-var-create - * "{escaped}"')
        if resp.get("message") == "error":
            return {"error": resp.get("payload", {}).get("msg", "invalid expression")}
        payload = resp.get("payload", {})
        name = payload.get("name")
        if name:
            self._watches[name] = expr
        return {"name": name, "expr": expr, "type": payload.get("type"), "value": payload.get("value")}

    def remove_watch(self, name: str) -> None:
        self._watches.pop(name, None)
        self._send_and_wait(f"-var-delete {name}")

    def update_watches(self) -> list[dict[str, Any]]:
        if not self._watches:
            return []
        resp = self._send_and_wait("-var-update --all-values *")
        if resp.get("message") == "error":
            return []
        changes = resp.get("payload", {}).get("changelist", [])
        for change in changes:
            change["expr"] = self._watches.get(change.get("name"), "")
        return changes

    # ---- internals -------------------------------------------------------

    def _exit_code(self) -> int | None:
        """None while GDB is running; its returncode once it has exited."""
        proc = self._controller.gdb_process
        if proc is None:
            return -1
        return proc.poll()

    def _check_alive(self) -> None:
        rc = self._exit_code()
        if rc is not None:
            raise GdbProcessExited(f"GDB is not running (exit code {rc})")

    def _write(self, cmd: str, *, tokened: bool) -> int | None:
        self._check_alive()
        token = None
        if tokened:
            token = next(self._token_counter)
            cmd = f"{token}{cmd}"
        with self._write_lock:
            self._controller.write(cmd, timeout_sec=0, read_response=False)
        return token

    def _send_and_wait(self, cmd: str, timeout: float = REGISTER_TIMEOUT) -> dict[str, Any]:
        self._check_alive()
        token = next(self._token_counter)
        ev = Event()
        self._pending[token] = ev
        with self._write_lock:
            self._controller.write(f"{token}{cmd}", timeout_sec=0, read_response=False)
        try:
            with eventlet.Timeout(timeout):
                return ev.wait()
        except eventlet.Timeout:
            raise GdbCommandTimeout(f"GDB command timed out after {timeout}s: {cmd}")
        finally:
            self._pending.pop(token, None)

    def _target_running(self) -> bool | None:
        """GDB's view of the target: True running, False stopped, None if
        it can't tell (no thread yet, GDB unresponsive)."""
        try:
            resp = self._send_and_wait("-thread-info", timeout=2.0)
        except Exception:
            return None
        states = {t.get("state") for t in (resp.get("payload") or {}).get("threads", [])}
        if "running" in states:
            return True
        return False if states else None

    @staticmethod
    def _ok_or_raise(resp: dict[str, Any]) -> dict[str, Any]:
        if resp.get("message") == "error":
            raise RuntimeError((resp.get("payload") or {}).get("msg", "GDB error"))
        return resp

    def _console_retrying(self, cmd: str, attempts: int = 10) -> None:
        """Run a CLI command, retrying while GDB still says the target is
        running (an -exec-interrupt that hasn't landed yet) -- same reason
        as reset()'s retry loop."""
        mi = f'-interpreter-exec console "{cmd}"'
        for i in range(attempts):
            resp = self._send_and_wait(mi, timeout=2.0)
            if resp.get("message") != "error":
                return
            if i < attempts - 1:
                eventlet.sleep(0.2)
        self._ok_or_raise(resp)

    def _fail_pending(self, message: str) -> None:
        # Wake every caller blocked in _send_and_wait right away instead of
        # letting each one sit out its full REGISTER_TIMEOUT.
        for ev in list(self._pending.values()):
            if not ev.ready():
                ev.send_exception(GdbProcessExited(message))

    def _read_loop(self) -> None:
        while not self._stop_reader:
            # Once GDB's stdout hits EOF, pygdbmi's select() reports it
            # readable forever and read() returns b'' -- get_gdb_response
            # would spin there at 100% CPU. Check the process first.
            rc = self._exit_code()
            if rc is not None:
                self._handle_gdb_exit(rc)
                return
            try:
                responses = self._controller.get_gdb_response(timeout_sec=0.5, raise_error_on_timeout=False)
            except Exception:
                if self._stop_reader:
                    return
                eventlet.sleep(0.1)
                continue
            for resp in responses:
                self._dispatch(resp)

    def _handle_gdb_exit(self, rc: int) -> None:
        if self._stop_reader:
            return
        self._stop_reader = True
        self._reader_greenlet = None
        self._fail_pending(f"GDB exited unexpectedly (exit code {rc})")
        if self._on_exit is not None:
            self._on_exit(rc)

    def _dispatch(self, resp: dict[str, Any]) -> None:
        token = resp.get("token")
        if token is not None and token in self._pending:
            ev = self._pending[token]
            if not ev.ready():
                ev.send(resp)
            return
        if self._closing or self._muted:
            return
        self._on_event(_to_event(resp))


def _to_event(resp: dict[str, Any]) -> dict[str, Any]:
    if resp.get("type") == "notify" and resp.get("message") == "stopped":
        payload = resp.get("payload", {})
        frame = payload.get("frame", {})
        return {
            "event": "stopped",
            "reason": payload.get("reason"),
            "pc": frame.get("addr"),
            "frame": frame,
            # Only present once symbols are loaded (load_symbols); None
            # otherwise, since GDB has no source mapping without an ELF.
            "file": frame.get("file"),
            "line": frame.get("line"),
        }
    if resp.get("type") in ("console", "log", "target"):
        return {"event": "console", "text": resp.get("payload") or ""}
    return {"event": "raw", "data": resp}
