# lst-bot

[![Steam](https://img.shields.io/badge/Steam-000000?logo=steam&logoColor=white)](https://steamcommunity.com/groups/lst99)
[![Discord](https://img.shields.io/badge/Discord-5865F2?logo=discord&logoColor=white)](https://discord.gg/4N3aeNsFt8)

[English](README.md) | 简体中文

`lst-bot` 是一个给 Let's Starve Together 玩家群体用的通用 IM 机器人。

![LST logo](https://pub.starv.ing/logo.png)

Let's Starve Together（LST）是围绕《饥荒联机版》（Don't Starve Together, DST）形成的自发游戏群体。仓库由 LST 机器人应用、可复用异步 bot 包和独立客户端包组成。`src/lst_bot/` 承载当前业务实现，`pkgs/bot/` 负责 OneBot 接入、事件分发、依赖注入、定时任务和动作执行。

## 它会做什么

- 接入 NapCat / OneBot 11。
- 查询 DST 最新版本、Klei 大厅、房间详情和在线玩家。
- 管理 LST 使用的本机 DST 房间：存档、回档、重启、重置。
- 定时向 IM 群报告活跃房间。
- 用 Grok 4.5 + Dosu MCP 回答 DST 相关问题。

## 整体结构

```mermaid
flowchart LR
    QQ[QQ 群] <--> NapCat[NapCat / OneBot 11]
    NapCat <--> Bot[lst-bot app]

    Bot --> Framework[bot 包]
    Framework --> Router[事件路由 / 权限 / DI]
    Framework --> Scheduler[cron 定时任务]
    Framework --> Gateway[OneBot 网关]

    Bot --> Clients[clients]
    Clients --> Klei[Klei 大厅与版本数据]
    Clients --> DST[本机 DST systemd 房间]
    Clients --> AI[Grok 4.5 + Dosu MCP]
    Clients --> Hitokoto[一言缓存]
```

## 目录

- `pkgs/bot/`：独立的异步 bot 框架包。
- `pkgs/hitokoto/`：独立的一言客户端包。
- `pkgs/klei/`：独立的 Klei 大厅、版本和论坛客户端包。
- `pkgs/lst/`：独立的本机 DST 房间控制客户端包。
- `src/lst_bot/`：机器人业务入口、配置和问答 Agent。
- `systemd/`：NapCat 容器和 lst-bot 服务。
- `tests/`：根应用测试。
- 各 package 的测试放在对应 package 旁边。

## 配置

应用从运行目录读取 `.env`。开发和部署都以仓库根目录作为工作目录，所以通常放在 `.env`。

核心配置：

| 变量 | 用途 |
| --- | --- |
| `ONEBOT_WS_URL` | NapCat 的 OneBot 11 WebSocket 地址 |
| `ONEBOT_ACCESS_TOKEN` | OneBot 鉴权 |
| `BOT_ADMIN` | 管理员账号 ID 列表 |
| `BOT_CMD_PREFIXES` | 命令前缀 |
| `REPORT_GROUP_ID` | 定时报告 IM 群 |
| `KLEI_ACCESS_TOKEN` | Klei lobby/read token |
| `KLEI_HOST_ID` | LST 需要管理的 DST 主机 ID |
| `GEMINI_API_KEY` | 暂时保留的 Gemini 问答密钥 |
| `XAI_API_KEY` | Grok 4.5 问答 |
| `DOSU_MCP_ENDPOINT` | Dosu MCP 地址 |
| `DOSU_API_KEY` | Dosu 鉴权 |
| `HTTP_PROXY` | 外部请求代理 |
| `LOG_LEVEL` | 日志等级 |

## 开发

- `just sync`：同步所有 workspace 包并安装 hooks。
- `just dev`：启动机器人应用包。
- `just check`：运行 CI 风格检查。
- `just test`：运行完整测试链路。
- `just build`：检查并构建。

## 部署

默认按 `/srv/lst-bot` 和 `/srv/napcat` 设计。

`systemd/napcat.container` 是 Podman Quadlet 配置，会生成 `napcat.service`，并把 NapCat 的配置和 QQ 数据放到 `/srv/napcat/config`、`/srv/napcat/ntqq`。

`systemd/lst-bot.service` 负责启动机器人，工作目录是 `/srv/lst-bot`，并在 `napcat.service` 之后启动。

部署时只需要确认几件事：

1. 项目位于 `/srv/lst-bot`，并已运行 `just sync`。
2. NapCat 容器已启用，OneBot 11 WebSocket 可连。
3. `.env` 已填好 OneBot、Klei、Gemini、xAI、Dosu 和报告群配置。
4. 需要房间管理时，本机存在 `dst@<room>.service` 这类 DST 房间服务。
5. 运行用户有权限控制 lst-bot、NapCat 和 DST 房间服务。

如果路径不同，记得同步调整 systemd 文件里的 `WorkingDirectory`、`ExecStart` 和 NapCat volume。
