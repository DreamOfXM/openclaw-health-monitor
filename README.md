# OpenClaw Health Monitor

中文 | [English](#english)

## 中文

OpenClaw Health Monitor 是一个面向 OpenClaw Gateway 的本地守护与观测工具。
它的目标不是替代 OpenClaw 本身，而是提供一层更稳定、更容易操作的“用户守卫”能力：

- 持续监控 Gateway 是否真的可用，而不是只看端口是否占用
- 提供 Web 仪表盘，集中查看进程、错误、会话、版本和告警
- 提供受控的自动恢复能力
- 为未来扩展本地诊断、更多告警渠道、策略化恢复打下基础

当前版本不会自动重启你现有运行中的服务，代码修改后需要你自行决定何时切换。

License: MIT. See `LICENSE`.

## 目标定位

这个项目适合：

- 个人用户或小团队在本机长期运行 OpenClaw
- 希望有“傻瓜式”安装和简单 Web 界面
- 希望在故障时自动发现问题、自动重启、记录变更
- 后续希望接入更高级的问题定位能力

这个项目当前不做：

- 不把本地模型诊断作为第一阶段必需能力
- 不依赖复杂外部数据库
- 不默认执行破坏性恢复操作

## 当前架构

### 核心组件

- `guardian.py`
  负责守护、健康检查、告警、自动恢复、版本标记。

- `dashboard.py`
  提供本地 Web UI，暴露状态、变更、配置、重启和受控恢复入口。

- `monitor_config.py`
  共享配置加载层。支持：
  - `config.conf`：可公开的默认配置
  - `config.local.conf`：本地私有覆盖
  - 环境变量覆盖

- `state_store.py`
  基于 SQLite 的本地状态库，用于统一保存：
  - alerts
  - versions
  - change events
  - health samples

### 架构分层

1. Control plane
   `dashboard.py` + `guardian.py`

2. Integration plane
   Gateway health probe、进程管理、通知发送

3. State plane
   SQLite 状态库 + 兼容旧 JSON 文件

4. Future intelligence plane
   后续可接入本地模型做根因定位、日志解释、恢复建议

## 为什么现在加入本地数据库

结论：有必要，但只需要轻量 SQLite，不需要上重型数据库。

原因：

- 你已经有多类状态：告警、版本、变更、健康检查
- 这些状态天然适合时间序列和事件流存储
- SQLite 零依赖、易安装、适合本地单机
- 后续做“最近 24h 异常模式”“恢复效果统计”“问题定位上下文”时，会比散落 JSON 稳定很多

因此当前采用的是：

- 保留旧 JSON 兼容
- 新增 SQLite 作为统一状态底座

## 为什么暂时不接本地模型

不是不能做，而是不应该放在第一阶段。

当前更重要的是先把下面几件事做好：

- 健康检查准确
- 重启逻辑可预测
- 恢复策略有边界
- 状态数据可追踪
- 安装足够简单

等这些稳定之后，本地模型诊断才有意义。否则模型只是在解释不稳定系统的噪音。

建议的路线：

1. 先把监控、恢复、事件记录稳定
2. 再加基于规则的诊断
3. 最后再接本地模型做“解释 + 建议”，而不是直接把模型放进关键路径

## 安装

### 前置条件

- macOS
- 已安装并可运行 `openclaw`
- Python 3.9+
- 当前用户有权限运行本地 OpenClaw gateway

### 快速开始

```bash
cd ~/openclaw-health-monitor
./install.sh
./start.sh
```

如果你想分开启动：

```bash
cd ~/openclaw-health-monitor
./.venv/bin/python guardian.py
./.venv/bin/python dashboard.py
```

访问：

```text
http://127.0.0.1:8080
```

### 一键启动

```bash
cd ~/openclaw-health-monitor
./install.sh
./start.sh
```

### 安装脚本会做什么

- 检查 `python3`
- 检查 `openclaw` 是否在 PATH 中
- 创建本地虚拟环境 `.venv`
- 安装 `requirements.txt`
- 初始化 `logs/`、`change-logs/`、`data/`
- 生成 git 忽略的 `config.local.conf`

### Dashboard 能做什么

- 查看 Gateway 健康、错误、会话、版本与资源使用
- 查看最近变更日志
- 查看、创建、恢复配置快照

## 配置

### 公开配置

文件：`config.conf`

这个文件应该可以安全地进入开源仓库，只放默认值和非敏感配置。

### 本地私有配置

文件：`config.local.conf`

放这些内容：

- Webhook
- 私有开关
- 本地机器特有配置

示例：

```ini
DINGTALK_WEBHOOK="https://example.invalid/webhook"
FEISHU_WEBHOOK=""
```

也可以通过环境变量覆盖，例如：

```bash
export DINGTALK_WEBHOOK="https://example.invalid/webhook"
./.venv/bin/python guardian.py
```

## 安全边界

当前版本的默认安全原则：

- 不把 webhook 明文返回给前端
- 不把私有配置写入公开配置文件
- 不默认执行 destructive recovery
- 默认开启配置快照恢复

`ENABLE_SNAPSHOT_RECOVERY=true` 时：

- 会为关键 OpenClaw 配置保留本地快照
- `/api/emergency-recover` 会恢复最近一次快照并发起重启

`ENABLE_DESTRUCTIVE_RECOVERY=false` 时：

- 不会自动执行 `git reset --hard`
- 不会把 git 回滚作为默认恢复路径

只有你明确接受风险时，才应在本地覆盖中开启：

```ini
ENABLE_DESTRUCTIVE_RECOVERY=true
```

## 恢复策略

### 默认恢复链路

1. 检测 Gateway 健康失败
2. 等待短暂窗口，避免和人工操作冲突
3. 尝试受控重启
4. 如果仍失败，恢复最近一次配置快照
5. 如果仍失败，发出告警

### 非默认恢复链路

仅当显式开启 destructive recovery 时：

1. 允许 git stash / reset --hard
2. 允许 emergency recover

这条链路不建议作为开源项目默认行为。

## 当前存储

### SQLite

默认数据库路径：

```text
data/monitor.db
```

当前用于保存：

- alerts
- versions
- change events
- health samples

### Config Snapshots

默认快照目录：

```text
snapshots/
```

当前默认覆盖：

- `~/.openclaw/openclaw.json`
- `~/.openclaw/gateway.json`
- `~/.openclaw/workspace-*/AGENTS.md`
- `~/.openclaw/workspace-*/SOUL.md`

### 兼容旧文件

当前仍兼容这些旧文件：

- `alerts.json`
- `versions.json`
- `change-logs/*.json`

这样可以平滑迁移，不强迫现有使用者立即切换。

## 开源前必须做的事

1. 轮换历史上暴露过的 webhook / token
2. 清理 git 历史中的敏感信息
3. 增加最小测试集
4. 增加安装脚本和环境检查
5. 明确支持矩阵：
   - macOS 版本
   - Python 版本
   - OpenClaw 版本
6. 轮换 webhook 后再公开发布

推荐先执行：

```bash
cd ~/openclaw-health-monitor
./prepare_release.sh
```

## 推荐路线图

### Phase 1

- 稳定健康检查
- 稳定重启路径
- SQLite 状态库
- 中英 README

### Phase 2

- 配置快照恢复，替代 git destructive recovery
- 规则化诊断引擎
- Dashboard 历史趋势图
- 多通知渠道抽象

### Phase 3

- 本地模型辅助诊断
- 日志聚类与根因建议
- 自定义恢复策略插件

## 目录结构

```text
openclaw-health-monitor/
├── guardian.py
├── dashboard.py
├── monitor_config.py
├── state_store.py
├── snapshot_manager.py
├── install.sh
├── requirements.txt
├── prepare_release.sh
├── LICENSE
├── CONTRIBUTING.md
├── SECURITY.md
├── start.sh
├── health-monitor.sh
├── config.conf
├── config.local.conf        # 本地私有，不进 git
├── data/
│   └── monitor.db
├── snapshots/
├── change-logs/
├── logs/
├── tests/
└── README.md
```

## English

OpenClaw Health Monitor is a local guard-and-observability layer for OpenClaw Gateway.
It does not replace OpenClaw itself. It provides a more stable and more operable user-facing guardrail layer:

- continuously verify that the Gateway is actually healthy, not just listening on a port
- provide a local web dashboard for process, error, session, version, and alert visibility
- provide controlled recovery actions
- establish a foundation for future local diagnosis and extension points

This repository is being shaped toward an open-source-friendly layout. Runtime changes are not auto-applied; you decide when to switch.

License: MIT. See `LICENSE`.

## Architecture

Core components:

- `guardian.py`: watchdog, health probe, alerts, recovery, version marking
- `dashboard.py`: local web UI and operator actions
- `monitor_config.py`: layered config loader
- `state_store.py`: SQLite-backed state store

Layers:

1. Control plane: dashboard and guardian
2. Integration plane: Gateway probing, process control, notifications
3. State plane: SQLite plus legacy JSON compatibility
4. Future intelligence plane: optional local-model-assisted diagnosis

## Why SQLite now

Yes, a local database is worth it now, but only a lightweight one.

SQLite is a good fit because:

- the project already has multiple state streams
- it keeps installation simple
- it enables future trend analysis and diagnostic context
- it avoids introducing an external service dependency

## Why local models are not phase 1

Local model diagnosis is promising, but it should not be in the critical path yet.

First, the system needs:

- accurate health checks
- predictable restart behavior
- bounded recovery policies
- durable state and event history
- simple installation

After that, local models can help explain problems. Before that, they mostly explain noise.

## Installation

```bash
cd ~/openclaw-health-monitor
./install.sh
./start.sh
```

If you want to run them separately:

```bash
cd ~/openclaw-health-monitor
./.venv/bin/python guardian.py
./.venv/bin/python dashboard.py
```

Open:

```text
http://127.0.0.1:8080
```

The dashboard supports:

- health and process visibility
- change log browsing
- snapshot listing, creation, and targeted restore

## Config Model

- `config.conf`: safe tracked defaults
- `config.local.conf`: local private overrides
- environment variables: highest priority

Secrets should live in `config.local.conf` or env vars, not in tracked files.

Simple install now uses:

```bash
cd ~/openclaw-health-monitor
./install.sh
./start.sh
```

## Safety Model

By default:

- webhooks are not exposed to the frontend
- private config is not written into tracked config
- destructive recovery is disabled
- snapshot recovery is enabled

With `ENABLE_SNAPSHOT_RECOVERY=true`:

- the monitor keeps local snapshots of key OpenClaw config files
- `/api/emergency-recover` restores the latest snapshot and then starts recovery

To explicitly enable risky git-based recovery:

```ini
ENABLE_DESTRUCTIVE_RECOVERY=true
```

## Recovery Model

Default recovery chain:

1. detect failed Gateway health
2. wait briefly to avoid fighting manual operations
3. attempt a controlled restart
4. restore the latest config snapshot if restart still fails
5. alert the operator if recovery still fails

Snapshot directory:

```text
snapshots/
```

Default snapshot targets:

- `~/.openclaw/openclaw.json`
- `~/.openclaw/gateway.json`
- `~/.openclaw/workspace-*/AGENTS.md`
- `~/.openclaw/workspace-*/SOUL.md`

## Tests

```bash
cd ~/openclaw-health-monitor
python3 -m unittest discover -s tests -v
```

## Release Prep

```bash
cd ~/openclaw-health-monitor
./prepare_release.sh
```

Optional safe cleanup:

```bash
cd ~/openclaw-health-monitor
./prepare_release.sh fix
```

## Community Docs

- `CONTRIBUTING.md`
- `SECURITY.md`
- `LICENSE`
- `CHANGELOG.md`
- `RELEASE.md`

## Roadmap

Phase 1:

- stable probes
- stable restart path
- SQLite state
- bilingual docs

Phase 2:

- config snapshot recovery
- rule-based diagnosis
- historical dashboards
- notification abstraction

Phase 3:

- local-model-assisted diagnosis
- log clustering and root-cause suggestions
- pluggable recovery strategies
