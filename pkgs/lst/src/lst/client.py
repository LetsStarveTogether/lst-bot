from collections.abc import Iterable
from logging import getLogger
from pathlib import Path
from typing import Any

from pystemd.systemd1 import Manager

logger = getLogger(__name__)


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

    def send_console_command(self, room_ids: Iterable[int], command: str) -> None:
        room_values = tuple(room_ids)
        logger.info(
            "send DST console command: %s (%s)",
            ",".join(str(room_id) for room_id in room_values),
            command,
        )
        payload = command if command.endswith("\n") else f"{command}\n"
        for room_id in room_values:
            console_path = self.data_path / str(room_id) / "console"
            logger.debug("write DST console command: %s", console_path)
            console_path.write_text(payload, encoding="utf-8")

    def restart_rooms(self, room_ids: Iterable[int]) -> None:
        if self._systemd_manager is None:
            manager = Manager()
            manager.load()
            self._systemd_manager = manager.Manager
        room_values = tuple(room_ids)
        logger.info(
            "restart DST rooms: %s",
            ",".join(str(room_id) for room_id in room_values),
        )
        for room_id in room_values:
            unit = self.service_template_name + f"@{room_id}.service".encode()
            logger.debug("restart DST systemd unit: %s", unit)
            self._systemd_manager.RestartUnit(unit, self.systemd_mode)
