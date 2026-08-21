from pathlib import Path
from unittest.mock import Mock, call

import pytest
from lst import LstClient


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
