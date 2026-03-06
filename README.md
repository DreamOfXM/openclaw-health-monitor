# OpenClaw Health Monitor

中文 | [English](#english)

## 中文

OpenClaw Health Monitor 是一个面向 OpenClaw Gateway 的本地监控与恢复工具。
它会在本机拉起一整套最小运行面：

- `Gateway`：OpenClaw 对外提供能力的核心服务
- `Guardian`：后台守护进程，负责健康检查、异常识别、告警和受控恢复
- `Dashboard`：本地 Web 页面，用来查看状态、日志、问题定位和手动操作

目标很直接：

- 小白用户可以下载安装后直接启动和停止整套服务
- 技术用户可以看到完整的运行链路、状态数据和恢复机制

License: MIT. See `LICENSE`.

## 快速开始

### 方式一：脚本安装

前置条件：

- macOS
- 已安装并可运行 `openclaw`
- Python 3.9+

安装并启动：

```bash
cd ~/openclaw-health-monitor
./install.sh
./start.sh
```

这会尝试启动整套本地运行面：

- Gateway
- Guardian
- Dashboard

启动后会自动打开 Dashboard。

常用命令：

```bash
cd ~/openclaw-health-monitor
./start.sh
./status.sh
./verify.sh
./stop.sh
```

### 方式二：桌面 App

仓库提供 macOS 桌面 App 发布产物：

- `.dmg`
- `.app.zip`

桌面 App 的行为和脚本入口保持一致：

- 打开 App：自动拉起 Gateway、Guardian、Dashboard
- 退出 App：停止 Gateway、Guardian、Dashboard

当前桌面 App 依赖本机已经准备好 `~/openclaw-health-monitor` 仓库和运行环境。

## 给小白用户的理解方式

可以把这个项目理解成一个本地控制台：

- `Gateway` 负责真正干活
- `Guardian` 负责盯着 Gateway，发现异常就记录和恢复
- `Dashboard` 负责把状态和问题展示出来

日常只需要记住四个命令：

```bash
./install.sh
./start.sh
./status.sh
./stop.sh
```

## 架构说明

### 核心组件

- `guardian.py`
  后台守护进程。负责健康检查、异常识别、自动恢复、通知和变更记录。

- `dashboard.py`
  本地 Web UI。负责展示 Gateway / Guardian / Dashboard 状态、最近异常、内存归因、配置快照和操作入口。

- `desktop_runtime.sh`
  本地总控脚本。负责统一启动、停止、查询：
  - Gateway
  - Guardian
  - Dashboard

- `monitor_config.py`
  配置加载层。支持：
  - `config.conf`
  - `config.local.conf`
  - 环境变量覆盖

- `state_store.py`
  基于 SQLite 的本地状态库，用于保存：
  - alerts
  - versions
  - change events
  - health samples

### 运行模型

1. `./start.sh`
   调用 `desktop_runtime.sh start all`

2. `desktop_runtime.sh`
   依次拉起 Gateway、Guardian、Dashboard，并记录 PID 文件

3. `Guardian`
   持续检查 Gateway 是否正常、扫描运行时异常、记录变更并发送通知

4. `Dashboard`
   提供本地问题定位面板、最近异常、内存归因和快照操作

5. `./stop.sh`
   调用 `desktop_runtime.sh stop all`，停止整套本地运行面

## GitHub Actions

仓库内已经提供 macOS 构建 workflow：

- `.github/workflows/release.yml`
- `.github/release.yml`

它会在 GitHub Actions 上自动完成：

- 安装 Python 依赖
- 安装 `pnpm`
- 安装 Rust toolchain
- 运行测试
- 构建桌面 App
- 整理 `.dmg` 和 `.app.zip`
- 上传为 workflow artifacts

当仓库 push `v*` tag 时，workflow 会把 `release/` 里的文件自动附加到 GitHub Release。

推荐的发布步骤：

```bash
cd ~/openclaw-health-monitor
make test
make pake
make release
```

然后：

1. 更新 `CHANGELOG.md`
2. 检查 `RELEASE.md`
3. 打 `v*` tag 并 push
4. 等 GitHub Actions 自动上传 release artifacts

## 运行验证

完成安装或升级后，可按下面顺序验证本地监控是否正常工作。

### 1. 基础启动验证

确认三部分都能正常启动：

```bash
cd ~/openclaw-health-monitor
./preflight.sh
./start.sh
```

其中：

- `./preflight.sh` 只做切换前检查，不会启动或重启服务
- `./start.sh` 会真正启动 Gateway、Guardian 和 Dashboard
- `./status.sh` 会汇总 Guardian、Dashboard、Gateway 和 Dashboard API 状态
- `./stop.sh` 会停止 Gateway、Guardian 和 Dashboard

打开：

```text
http://127.0.0.1:8080
```

检查项：

- Dashboard 首页可以正常加载
- `Guardian` 和 `Gateway` 状态可见
- 最近异常区和问题定位区没有前端报错

### 2. 异常识别验证

关注这些场景是否会进入变更日志和首页异常区：

- `dispatch complete (queuedFinal=false, replies=0)` 会被识别为“任务完成但没有可见回复”
- `gateway closed (1006 ...)` 会被识别为 `gateway_ws_closed`
- `abort failed ... no_active_run` 会被识别为任务状态追踪异常
- 长时间只有 `dispatching to agent` 没有 `dispatch complete` 时，会出现“任务长时间无最终结果”
- 长时间停留在同一个 `PIPELINE_PROGRESS` 阶段时，会出现“任务阶段长时间无进展”

检查项：

- 首页“问题定位”能显示当前关注点、最后阶段、建议动作
- 首页“最近异常 / 进度”能看到异常时间、问题、耗时、阶段
- “变更日志”页能看到 `anomaly` 和 `pipeline` 事件

### 3. 内存归因验证

检查首页内存区是否满足“可对账”：

- 顶部内存卡片会显示总已用内存
- “内存归因：进程 Top 15 + 系统项”区域会显示：
  - `Top 15 进程`
  - `Kernel / Wired`
  - `Compressed`
  - `Other System`
- 页面会明确显示：
  - `Top 15` 覆盖了多少已用内存
  - 还有多少属于系统/缓存/未归属项

如果总内存很高，但进程榜单只解释一部分，这是预期行为；关键是页面现在应该把剩余部分解释出来，而不是只给总量。

### 4. 通知验证

如果已经配置钉钉或飞书 webhook，检查：

- 异常首次出现时会发送通知
- 同类异常在去重窗口内不会刷屏

### 5. 快速回归验证

如需运行本地测试，可以执行：

```bash
python3 -m unittest discover -s tests
```

如需在重启后快速做一轮在线验收，可以直接运行：

```bash
cd ~/openclaw-health-monitor
./verify.sh
```

`./verify.sh` 会自动探测 `8080-8089` 之间实际被 Dashboard 占用的端口。

当前测试覆盖了：

- 配置加载
- SQLite 状态存储
- 配置快照
- Guardian 运行时异常识别与去重
- Dashboard 内存归因计算

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

仅在明确接受风险时，才应在本地覆盖中开启：

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

## 发布前检查清单

1. 轮换已暴露或疑似暴露的 webhook / token
2. 清理 git 历史中的敏感信息
3. 运行最小测试集并确认通过
4. 确认安装脚本和环境检查可用
5. 明确支持矩阵：
   - macOS 版本
   - Python 版本
   - OpenClaw 版本

如需做一轮发布前检查，可先执行：

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
├── Makefile
├── build_pake_prototype.sh
├── package_release.sh
├── start.sh
├── status.sh
├── stop.sh
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

This repository is structured for public distribution. Runtime changes are not auto-applied; switching remains explicit.

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
4. Future extension plane: additional diagnosis, analytics, and recovery features

## Current state storage

Current approach:

- keep legacy JSON compatibility
- add SQLite as the primary local state layer

## Installation

```bash
cd ~/openclaw-health-monitor
./install.sh
./start.sh
```

To stop the local monitor components:

```bash
cd ~/openclaw-health-monitor
./stop.sh
```

To inspect local runtime status:

```bash
cd ~/openclaw-health-monitor
./status.sh
```

You can also use the unified targets:

```bash
cd ~/openclaw-health-monitor
make preflight
make start
make status
make verify
make stop
make test
make pake
make release
```

To build the desktop app bundle:

```bash
cd ~/openclaw-health-monitor
./build_pake_prototype.sh
```

To run the components separately:

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

- log clustering and root-cause suggestions
- pluggable recovery strategies
