"""Board profiles: pick an OpenOCD interface/target cfg pair + GDB binary per board.

Board support is data, not code — GDB's machine interface is the same
regardless of target, so adding a new Cortex-M board is normally just a
new entry here (verify the exact `target/*.cfg` matches your chip variant;
run `openocd -f interface/<x>.cfg -f target/<y>.cfg` by hand once to check
before wiring a board up here).

Ported from the standalone Debugger/ prototype (backend/app/boards.py),
unchanged apart from adding nucleo_f446re and black_pill — both Cortex-M
boards this fleet already lists in the Board dropdown (see app.py) that
happen to be ST-Link-compatible like the existing "stm32" entry, just a
different target cfg for their specific chip family.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class BoardProfile:
    id: str
    label: str
    openocd_interface: str
    openocd_target: str
    gdb_binary: str
    # Forced transport, passed as `transport select <value>` between the
    # interface and target cfg. Needed for probes whose OpenOCD driver only
    # implements one transport but auto-selection guesses the other (e.g.
    # TI's ICDI driver is JTAG-only; OpenOCD defaults it to SWD and fails).
    openocd_transport: str | None = None
    # OpenOCD command debug_stop uses to leave the target running for the
    # next student -- a reset (not just a resume) so it restarts cleanly
    # from the flashed firmware's entry point instead of mid-breakpoint.
    resume_monitor_cmd: str = "reset run"


BOARDS: dict[str, BoardProfile] = {
    "stm32": BoardProfile(
        id="stm32",
        label="STM32 (Cortex-M, ST-Link)",
        openocd_interface="interface/stlink.cfg",
        # Adjust to your exact chip family, e.g. target/stm32f4x.cfg for F4 parts.
        openocd_target="target/stm32f1x.cfg",
        # Raspberry Pi OS's gdb-multiarch understands ARM targets out of the
        # box; a cross-compiled arm-none-eabi-gdb also works if installed.
        gdb_binary="gdb-multiarch",
    ),
    "nucleo_f446re": BoardProfile(
        id="nucleo_f446re",
        label="ST Nucleo-F446RE (STM32F4, onboard ST-Link)",
        openocd_interface="interface/stlink.cfg",
        openocd_target="target/stm32f4x.cfg",
        gdb_binary="gdb-multiarch",
    ),
    "black_pill": BoardProfile(
        id="black_pill",
        label="Black Pill (STM32F4x1, ST-Link/CMSIS-DAP)",
        openocd_interface="interface/stlink.cfg",
        openocd_target="target/stm32f4x.cfg",
        gdb_binary="gdb-multiarch",
    ),
    "tiva": BoardProfile(
        id="tiva",
        label="TI Tiva C Series (Cortex-M, onboard ICDI)",
        # TM4C123 LaunchPads use the onboard Luminary/TI ICDI debugger
        # (USB id 1cbe:00fd), not a separate ST-Link probe. Its OpenOCD
        # driver (ti_icdi_usb.c) only implements JTAG, so transport must be
        # forced — auto-selection defaults to SWD and fails to open.
        openocd_interface="interface/ti-icdi.cfg",
        # Verified against this OS's actual openocd scripts dir -- it's
        # target/stellaris.cfg, not target/ti/stellaris.cfg (no such 'ti'
        # subdirectory exists under target/ in stock OpenOCD script trees).
        openocd_target="target/stellaris.cfg",
        openocd_transport="jtag",
        gdb_binary="gdb-multiarch",
    ),
}


def get_board(board_id: str) -> BoardProfile:
    try:
        return BOARDS[board_id]
    except KeyError:
        raise ValueError(f"Unknown board id: {board_id!r}. Known boards: {list(BOARDS)}")
