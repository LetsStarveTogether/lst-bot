from __future__ import annotations

from abc import ABC
from configparser import ConfigParser
from ipaddress import IPv4Address
from pathlib import Path
from typing import Self

from logbook import Logger
from pydantic import BaseModel, Field

logger = Logger(__name__)


class ClusterMisc(BaseModel):
    max_snapshots: int = 6
    console_enabled: bool = True


class ClusterShard(BaseModel):
    shard_enabled: bool = False
    bind_ip: IPv4Address = IPv4Address("127.0.0.1")
    master_ip: IPv4Address = IPv4Address("127.0.0.1")
    master_port: int = 10888
    cluster_key: str = "defaultPass"


class ClusterSteam(BaseModel):
    steam_group_only: bool = False
    steam_group_id: int = 0
    steam_group_admins: bool = False


class ClusterNetwork(BaseModel):
    cluster_name: str = ""
    cluster_password: str = ""
    cluster_description: str = ""
    tick_rate: int = 15
    offline_cluster: bool = False
    lan_only_cluster: bool = False
    autosaver_enabled: bool = True
    whitelist_slots: int = 0
    cluster_language: str = "en"


class ClusterGameplay(BaseModel):
    max_players: int = 16
    pvp: bool = False
    pause_when_empty: bool = True
    vote_enabled: bool = True


class ServerShard(BaseModel):
    is_master: bool = True
    name: str = "[SHDMASTER]"
    id: int = 1


class ServerSteam(BaseModel):
    authentication_port: int = 8766
    master_server_port: int = 27016


class ServerNetwork(BaseModel):
    server_port: int = 10999


class ServerAccount(BaseModel):
    encode_user_path: bool = False


class IniModel(BaseModel, ABC):
    @classmethod
    def load(cls, ini_path: Path) -> Self:
        if __debug__:
            logger.debug(
                "load ini model : {model} {path}",
                model=cls.__name__,
                path=ini_path,
            )
        parser = ConfigParser()
        with ini_path.open(mode="r", encoding="utf-8") as file:
            parser.read_file(file)
        config_dict = {
            section: dict(parser.items(section)) for section in parser.sections()
        }
        return cls.model_validate(config_dict)

    def save(self, ini_path: Path) -> None:
        if __debug__:
            logger.debug(
                "save ini model : {model} {path}",
                model=type(self).__name__,
                path=ini_path,
            )
        config_dict = self.model_dump()
        for section_dict in config_dict.values():
            for key, value in section_dict.items():
                if isinstance(value, bool):
                    section_dict[key] = str(value).lower()

        parser = ConfigParser()
        parser.read_dict(config_dict)
        ini_path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        with ini_path.open(mode="w", encoding="utf-8") as file:
            parser.write(file)


class ClusterConfig(IniModel):
    misc: ClusterMisc = Field(default_factory=ClusterMisc)
    shard: ClusterShard = Field(default_factory=ClusterShard)
    steam: ClusterSteam = Field(default_factory=ClusterSteam)
    network: ClusterNetwork = Field(default_factory=ClusterNetwork)
    gameplay: ClusterGameplay = Field(default_factory=ClusterGameplay)


class ServerConfig(IniModel):
    shard: ServerShard = Field(default_factory=ServerShard)
    steam: ServerSteam = Field(default_factory=ServerSteam)
    network: ServerNetwork = Field(default_factory=ServerNetwork)
    account: ServerAccount = Field(default_factory=ServerAccount)


__all__ = [
    "ClusterConfig",
    "ClusterGameplay",
    "ClusterMisc",
    "ClusterNetwork",
    "ClusterShard",
    "ClusterSteam",
    "IniModel",
    "ServerAccount",
    "ServerConfig",
    "ServerNetwork",
    "ServerShard",
    "ServerSteam",
]
