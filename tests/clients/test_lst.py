from __future__ import annotations

from pathlib import Path

from lst_bot.clients.lst import ClusterConfig, LstClient, ServerConfig


class FakeSystemdInterface:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bytes, bytes]] = []

    def StartUnit(self, unit_name: bytes, mode: bytes) -> bytes:  # noqa: N802
        self.calls.append(("StartUnit", unit_name, mode))
        return b"/job/start"

    def StopUnit(self, unit_name: bytes, mode: bytes) -> bytes:  # noqa: N802
        self.calls.append(("StopUnit", unit_name, mode))
        return b"/job/stop"

    def RestartUnit(self, unit_name: bytes, mode: bytes) -> bytes:  # noqa: N802
        self.calls.append(("RestartUnit", unit_name, mode))
        return b"/job/restart"


class BlockingSystemdClient(LstClient):
    @property
    def systemd_manager(self) -> object:
        msg = "systemd manager should not be loaded"
        raise AssertionError(msg)


def test_lst_client_writes_console_commands(tmp_path: Path) -> None:
    (tmp_path / "1").mkdir()
    (tmp_path / "2").mkdir()
    client = LstClient(data_path=tmp_path)

    client.save_rooms([1, 2])

    paths = [tmp_path / "1" / "console", tmp_path / "2" / "console"]
    assert paths[0].read_text(encoding="utf-8") == "c_save()\n"
    assert paths[1].read_text(encoding="utf-8") == "c_save()\n"


def test_lst_client_console_commands_do_not_load_systemd(tmp_path: Path) -> None:
    (tmp_path / "1").mkdir()
    client = BlockingSystemdClient(data_path=tmp_path)

    client.save_rooms([1])

    assert (tmp_path / "1" / "console").read_text(encoding="utf-8") == "c_save()\n"


def test_lst_client_formats_console_commands(tmp_path: Path) -> None:
    (tmp_path / "1").mkdir()
    client = LstClient(data_path=tmp_path)

    client.rollback_rooms([1], 3)
    assert (tmp_path / "1" / "console").read_text(encoding="utf-8") == (
        "c_rollback(3)\n"
    )

    client.regenerate_rooms([1])
    assert (tmp_path / "1" / "console").read_text(encoding="utf-8") == (
        "c_regenerateworld()\n"
    )


def test_lst_ini_configs_round_trip_typed_values(tmp_path: Path) -> None:
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


def test_lst_client_controls_dst_systemd_units_without_waiting(
    tmp_path: Path,
) -> None:
    manager = FakeSystemdInterface()
    client = LstClient(
        data_path=tmp_path,
        systemd_manager=manager,
    )

    client.restart_rooms([1, 2])
    client.stop_rooms([3])
    client.start_rooms([4])

    assert manager.calls == [
        ("RestartUnit", b"dst@1.service", b"replace"),
        ("RestartUnit", b"dst@2.service", b"replace"),
        ("StopUnit", b"dst@3.service", b"replace"),
        ("StartUnit", b"dst@4.service", b"replace"),
    ]


def test_lst_client_accepts_custom_service_template_name(tmp_path: Path) -> None:
    manager = FakeSystemdInterface()
    client = LstClient(
        data_path=tmp_path,
        service_template_name=b"custom-dst",
        systemd_manager=manager,
    )

    client.restart_rooms([12])

    assert manager.calls == [
        ("RestartUnit", b"custom-dst@12.service", b"replace"),
    ]
