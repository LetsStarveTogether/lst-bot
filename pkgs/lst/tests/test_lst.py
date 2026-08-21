from collections.abc import Callable
from pathlib import Path
from unittest.mock import Mock, call

import pytest
from lst import LstClient


@pytest.mark.parametrize(
    ("operation", "expected"),
    [
        (lambda client, rooms: client.save_rooms(rooms), "c_save()\n"),
        (lambda client, rooms: client.rollback_rooms(rooms, 3), "c_rollback(3)\n"),
        (
            lambda client, rooms: client.regenerate_rooms(rooms),
            "c_regenerateworld()\n",
        ),
    ],
)
def test_console_operations_write_each_room(
    tmp_path: Path,
    operation: Callable[[LstClient, list[int]], None],
    expected: str,
) -> None:
    for room_id in (1, 2):
        (tmp_path / str(room_id)).mkdir()
    client = LstClient(data_path=tmp_path)

    operation(client, [1, 2])

    assert (tmp_path / "1" / "console").read_text(encoding="utf-8") == expected
    assert (tmp_path / "2" / "console").read_text(encoding="utf-8") == expected


@pytest.mark.parametrize(
    ("command", "expected"),
    [("say('hello')", "say('hello')\n"), ("say('hello')\n", "say('hello')\n")],
)
def test_send_console_command_writes_exactly_one_trailing_newline(
    tmp_path: Path,
    command: str,
    expected: str,
) -> None:
    (tmp_path / "1").mkdir()
    client = LstClient(data_path=tmp_path)

    client.send_console_command((1,), command)

    assert (tmp_path / "1" / "console").read_text(encoding="utf-8") == expected


def test_restart_rooms_passes_exact_unit_and_mode() -> None:
    manager = Mock()
    client = LstClient(
        service_template_name=b"custom-dst",
        systemd_mode=b"fail",
        systemd_manager=manager,
    )

    client.restart_rooms([1, 12])

    assert manager.method_calls == [
        call.RestartUnit(b"custom-dst@1.service", b"fail"),
        call.RestartUnit(b"custom-dst@12.service", b"fail"),
    ]
