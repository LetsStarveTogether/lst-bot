from __future__ import annotations

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

    def service_name(self, room_id: int) -> bytes:
        return self.service_template_name + b"@" + str(room_id).encode() + b".service"

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
                logger.debug(
                    "write DST console command : {path}",
                    path=console_path,
                )
            console_path.write_text(payload, encoding="utf-8")

    def save_rooms(self, room_ids: Iterable[int]) -> None:
        self.send_console_command(room_ids, "c_save()")

    def rollback_rooms(self, room_ids: Iterable[int], days: int) -> None:
        self.send_console_command(room_ids, f"c_rollback({days})")

    def regenerate_rooms(self, room_ids: Iterable[int]) -> None:
        self.send_console_command(room_ids, "c_regenerateworld()")

    def start_rooms(self, room_ids: Iterable[int]) -> None:
        systemd_manager = self.systemd_manager
        room_values = tuple(room_ids)
        logger.info(
            "start DST rooms: {rooms}",
            rooms=",".join(str(room_id) for room_id in room_values),
        )
        for room_id in room_values:
            unit = self.service_name(room_id)
            if __debug__:
                logger.debug("start DST systemd unit : {unit}", unit=unit)
            systemd_manager.StartUnit(unit, self.systemd_mode)

    def stop_rooms(self, room_ids: Iterable[int]) -> None:
        systemd_manager = self.systemd_manager
        room_values = tuple(room_ids)
        logger.info(
            "stop DST rooms: {rooms}",
            rooms=",".join(str(room_id) for room_id in room_values),
        )
        for room_id in room_values:
            unit = self.service_name(room_id)
            if __debug__:
                logger.debug("stop DST systemd unit : {unit}", unit=unit)
            systemd_manager.StopUnit(unit, self.systemd_mode)

    def restart_rooms(self, room_ids: Iterable[int]) -> None:
        systemd_manager = self.systemd_manager
        room_values = tuple(room_ids)
        logger.info(
            "restart DST rooms: {rooms}",
            rooms=",".join(str(room_id) for room_id in room_values),
        )
        for room_id in room_values:
            unit = self.service_name(room_id)
            if __debug__:
                logger.debug("restart DST systemd unit : {unit}", unit=unit)
            systemd_manager.RestartUnit(unit, self.systemd_mode)
