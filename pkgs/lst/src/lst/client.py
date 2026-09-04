import os
from collections.abc import Iterable
from logging import getLogger
from pathlib import Path
from select import PIPE_BUF
from stat import S_ISFIFO
from typing import Any

from pystemd.systemd1 import Manager

logger = getLogger(__name__)


def validate_room_ids(room_ids: Iterable[str]) -> list[str]:
    values = list(room_ids)
    if not values or any(
        not isinstance(value, str) or not value.isascii() or not value.isdecimal()
        for value in values
    ):
        msg = "room ids must be nonempty ASCII decimal strings"
        raise ValueError(msg)
    return list(dict.fromkeys(values))


class LstClient:
    def __init__(
        self,
        *,
        data_path: Path = Path("/srv/dst"),
        service_template_name: bytes = b"dst",
        systemd_mode: bytes = b"replace",
        systemd_manager: Any | None = None,
    ) -> None:
        self.data_path = data_path
        self.service_template_name = service_template_name
        self.systemd_mode = systemd_mode
        self._systemd_manager = systemd_manager

    def send_console_command(self, room_ids: Iterable[str], command: str) -> None:
        room_values = validate_room_ids(room_ids)
        payload = (command if command.endswith("\n") else f"{command}\n").encode()
        if len(payload) > PIPE_BUF:
            msg = f"DST console command exceeds {PIPE_BUF} bytes"
            raise ValueError(msg)
        logger.info(
            "send DST console command: %s (%s)",
            ",".join(room_values),
            command,
        )
        for room_id in room_values:
            console_path = self.data_path / room_id / "console"
            logger.debug("write DST console command: %s", console_path)
            descriptor = os.open(console_path, os.O_WRONLY | os.O_NONBLOCK)
            try:
                if not S_ISFIFO(os.fstat(descriptor).st_mode):
                    msg = f"DST console is not a FIFO: {console_path}"
                    raise OSError(msg)
                # A nonblocking write of at most PIPE_BUF bytes is all-or-nothing.
                os.write(descriptor, payload)
            finally:
                os.close(descriptor)

    def restart_rooms(self, room_ids: Iterable[str]) -> None:
        room_values = validate_room_ids(room_ids)
        if self._systemd_manager is None:
            manager = Manager()
            manager.load()
            self._systemd_manager = manager.Manager
        logger.info(
            "restart DST rooms: %s",
            ",".join(room_values),
        )
        for room_id in room_values:
            unit = self.service_template_name + f"@{room_id}.service".encode()
            logger.debug("restart DST systemd unit: %s", unit)
            self._systemd_manager.RestartUnit(unit, self.systemd_mode)
