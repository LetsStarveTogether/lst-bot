import os
from contextlib import suppress
from errno import ENXIO
from pathlib import Path
from select import PIPE_BUF
from unittest.mock import Mock, call

import pytest
from lst import LstClient, validate_room_ids


@pytest.fixture
def console_path(tmp_path: Path) -> Path:
    path = tmp_path / "020" / "console"
    path.parent.mkdir()
    os.mkfifo(path)
    return path


@pytest.mark.parametrize(
    ("command", "expected"),
    [("say('hello')", "say('hello')\n"), ("say('hello')\n", "say('hello')\n")],
)
def test_send_console_command_writes_exactly_one_trailing_newline(
    console_path: Path,
    command: str,
    expected: str,
) -> None:
    client = LstClient(data_path=console_path.parent.parent)
    reader = os.open(console_path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        client.send_console_command(("020", "020"), command)
        assert os.read(reader, PIPE_BUF) == expected.encode()
    finally:
        os.close(reader)


def test_console_without_reader_fails_without_blocking(console_path: Path) -> None:
    client = LstClient(data_path=console_path.parent.parent)
    with pytest.raises(OSError, match="No such device or address") as error:
        client.send_console_command(("020",), "c_save()")
    assert error.value.errno == ENXIO


@pytest.mark.parametrize("existing", [False, True])
def test_console_does_not_create_or_overwrite_regular_files(
    tmp_path: Path, existing: bool
) -> None:
    path = tmp_path / "020" / "console"
    path.parent.mkdir()
    if existing:
        path.write_text("keep", encoding="utf-8")
    client = LstClient(data_path=tmp_path)

    with pytest.raises(OSError, match="not a FIFO" if existing else "No such file"):
        client.send_console_command(("020",), "c_save()")

    assert path.exists() is existing
    if existing:
        assert path.read_text(encoding="utf-8") == "keep"


def test_console_writes_are_atomic_and_never_wait_for_space(console_path: Path) -> None:
    client = LstClient(data_path=console_path.parent.parent)
    reader = os.open(console_path, os.O_RDONLY | os.O_NONBLOCK)
    writer = os.open(console_path, os.O_WRONLY | os.O_NONBLOCK)
    try:
        with pytest.raises(ValueError, match="exceeds"):
            client.send_console_command(("020",), "x" * PIPE_BUF)
        with pytest.raises(BlockingIOError):
            os.read(reader, PIPE_BUF)
        with suppress(BlockingIOError):
            while True:
                os.write(writer, b"x" * PIPE_BUF)
        with pytest.raises(BlockingIOError):
            client.send_console_command(("020",), "c_save()")
    finally:
        os.close(writer)
        os.close(reader)


@pytest.mark.parametrize("invalid", ["", "-1", "+1", "1/../2", "\uff11\uff12", " 1"])
def test_room_ids_are_validated_before_any_side_effect(
    tmp_path: Path, invalid: str
) -> None:
    manager = Mock()
    client = LstClient(data_path=tmp_path, systemd_manager=manager)

    with pytest.raises(ValueError, match="room ids"):
        client.send_console_command(("020", invalid), "c_save()")
    with pytest.raises(ValueError, match="room ids"):
        client.restart_rooms(("020", invalid))
    assert manager.method_calls == []


def test_room_ids_keep_exact_spelling_and_first_seen_order() -> None:
    assert validate_room_ids(("000", "020", "0", "000")) == ["000", "020", "0"]
    with pytest.raises(ValueError, match="room ids"):
        validate_room_ids(())


def test_restart_rooms_passes_exact_unit_and_mode() -> None:
    manager = Mock()
    client = LstClient(
        service_template_name=b"custom-dst",
        systemd_mode=b"fail",
        systemd_manager=manager,
    )

    client.restart_rooms(["000", "020", "020"])

    assert manager.method_calls == [
        call.RestartUnit(b"custom-dst@000.service", b"fail"),
        call.RestartUnit(b"custom-dst@020.service", b"fail"),
    ]
