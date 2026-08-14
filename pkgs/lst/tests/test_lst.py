from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from unittest.mock import Mock, call

import pytest
from lst import ClusterConfig, LstClient, ServerConfig


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
    ids=["save", "rollback", "regenerate"],
)
def test_console_operations_write_each_room(
    tmp_path: Path,
    operation: Callable[[LstClient, list[int]], None],
    expected: str,
) -> None:
    for room_id in (1, 2):
        (tmp_path / str(room_id)).mkdir()
    client = LstClient(
        data_path=tmp_path,
        systemd_manager=object(),
    )

    operation(client, [1, 2])

    assert (tmp_path / "1" / "console").read_text(encoding="utf-8") == expected
    assert (tmp_path / "2" / "console").read_text(encoding="utf-8") == expected


@pytest.mark.parametrize(
    ("command", "expected"),
    [("say('hello')", "say('hello')\n"), ("say('hello')\n", "say('hello')\n")],
    ids=["append-newline", "preserve-newline"],
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


@pytest.mark.parametrize(
    ("operation", "method"),
    [
        (lambda client, rooms: client.start_rooms(rooms), "StartUnit"),
        (lambda client, rooms: client.stop_rooms(rooms), "StopUnit"),
        (lambda client, rooms: client.restart_rooms(rooms), "RestartUnit"),
    ],
    ids=["start", "stop", "restart"],
)
def test_systemd_operations_pass_exact_unit_and_mode(
    tmp_path: Path,
    operation: Callable[[LstClient, list[int]], None],
    method: str,
) -> None:
    manager = Mock()
    client = LstClient(
        data_path=tmp_path,
        service_template_name=b"custom-dst",
        systemd_mode=b"fail",
        systemd_manager=manager,
    )

    operation(client, [1, 12])

    assert manager.method_calls == [
        getattr(call, method)(b"custom-dst@1.service", b"fail"),
        getattr(call, method)(b"custom-dst@12.service", b"fail"),
    ]


def test_ini_configs_round_trip_typed_values(tmp_path: Path) -> None:
    cluster_path = tmp_path / "Cluster_1" / "cluster.ini"
    server_path = tmp_path / "Cluster_1" / "Master" / "server.ini"
    cluster = ClusterConfig.model_validate({
        "misc": {"console_enabled": False},
        "shard": {"shard_enabled": True, "bind_ip": "127.0.0.2"},
        "network": {
            "cluster_name": "Main",
            "offline_cluster": True,
            "whitelist_slots": 2,
        },
    })
    server = ServerConfig.model_validate({
        "shard": {"is_master": False, "name": "Caves", "id": 2},
        "network": {"server_port": 11000},
    })

    cluster.save(cluster_path)
    server.save(server_path)

    loaded_cluster = ClusterConfig.load(cluster_path)
    loaded_server = ServerConfig.load(server_path)
    assert "console_enabled = false" in cluster_path.read_text(encoding="utf-8")
    assert loaded_cluster.misc.console_enabled is False
    assert loaded_cluster.shard.shard_enabled is True
    assert str(loaded_cluster.shard.bind_ip) == "127.0.0.2"
    assert loaded_cluster.network.cluster_name == "Main"
    assert loaded_cluster.network.offline_cluster is True
    assert loaded_cluster.network.whitelist_slots == 2
    assert loaded_server.shard.is_master is False
    assert loaded_server.shard.name == "Caves"
    assert loaded_server.shard.id == 2
    assert loaded_server.network.server_port == 11000
