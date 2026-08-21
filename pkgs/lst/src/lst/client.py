from collections.abc import Iterable
from pathlib import Path
from typing import Any

from logbook import Logger
from pystemd.systemd1 import Manager

logger = Logger(__name__)


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

    @property
    def systemd_manager(self) -> Any:
        if self._systemd_manager is None:
            manager = Manager()
            manager.load()
            self._systemd_manager = manager.Manager
        return self._systemd_manager

    def send_console_command(self, room_ids: Iterable[int], command: str) -> None:
        room_values = tuple(room_ids)
        logger.info(
            "send DST console command: {rooms} ({command})",
            rooms=",".join(str(room_id) for room_id in room_values),
            command=command,
        )
        payload = command if command.endswith("\n") else f"{command}\n"
        for room_id in room_values:
            console_path = self.data_path / str(room_id) / "console"
            if __debug__:
                logger.debug("write DST console command : {path}", path=console_path)
            console_path.write_text(payload, encoding="utf-8")

    def restart_rooms(self, room_ids: Iterable[int]) -> None:
        systemd_manager = self.systemd_manager
        room_values = tuple(room_ids)
        logger.info(
            "restart DST rooms: {rooms}",
            rooms=",".join(str(room_id) for room_id in room_values),
        )
        for room_id in room_values:
            unit = self.service_template_name + f"@{room_id}.service".encode()
            if __debug__:
                logger.debug("restart DST systemd unit : {unit}", unit=unit)
            systemd_manager.RestartUnit(unit, self.systemd_mode)
