# OpenClaw Health Monitor

<p align="center">
  <img src="./assets/readme/hero.png" alt="OpenClaw Health Monitor" width="100%" />
</p>

<p align="center">
  OpenClaw Gateway 的本地监控、诊断与恢复控制台
</p>

<p align="center">
  <a href="https://github.com/DreamOfXM/openclaw-health-monitor/releases/latest">
    <img src="https://img.shields.io/github/v/release/DreamOfXM/openclaw-health-monitor?style=for-the-badge&color=2f6feb&label=Release" alt="Release" />
  </a>
  <a href="https://github.com/DreamOfXM/openclaw-health-monitor/releases/latest/download/openclaw-health-monitor-macos-arm64.dmg">
    <img src="https://img.shields.io/badge/macOS-dmg%20Download-1f883d?style=for-the-badge&logo=apple" alt="Download dmg" />
  </a>
  <a href="https://github.com/DreamOfXM/openclaw-health-monitor/releases/latest/download/openclaw-health-monitor-macos-arm64.app.zip">
    <img src="https://img.shields.io/badge/Desktop-App%20Zip-f97316?style=for-the-badge" alt="Download App Zip" />
  </a>
  <a href="https://github.com/DreamOfXM/openclaw-health-monitor/actions/workflows/release.yml">
    <img src="https://img.shields.io/github/actions/workflow/status/DreamOfXM/openclaw-health-monitor/release.yml?style=for-the-badge&label=Release%20Build" alt="Release Build" />
  </a>
  <a href="https://github.com/DreamOfXM/openclaw-health-monitor/blob/main/LICENSE">
    <img src="https://img.shields.io/badge/License-MIT-a855f7?style=for-the-badge" alt="MIT License" />
  </a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/macOS-Apple%20Silicon%20Ready-111827?style=flat-square&logo=apple" alt="macOS" />
  <img src="https://img.shields.io/badge/Python-3.9%2B-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python" />
  <img src="https://img.shields.io/badge/OpenClaw-Gateway%20Runtime-0F766E?style=flat-square" alt="OpenClaw Gateway" />
  <img src="https://img.shields.io/badge/Guardian-Health%20Watch-E11D48?style=flat-square" alt="Guardian" />
  <img src="https://img.shields.io/badge/Dashboard-Local%20Control%20Plane-F59E0B?style=flat-square" alt="Dashboard" />
</p>

中文 | [English](#english)

## 中文

OpenClaw Health Monitor 是一个面向 OpenClaw Gateway 的本地监控与恢复工具。  
它把 `Gateway`、`Guardian`、`Dashboard` 组合成一套可以直接启动、直接停止、直接定位问题的本地控制台。

它兼容两类 OpenClaw 运行方式：

- 单 agent：关注是否长时间无最终回复、是否出现无可见回复、是否需要主动进度播报
- 多 agent：在上述基础上继续识别阶段切换、阶段停滞、长任务升级播报

适合两类人：

- 小白用户：下载后直接启动，看到当前是否正常、哪里异常、要不要处理
- 技术用户：查看运行链路、异常归因、恢复动作、内存归因和本地状态

## 快速开始

### 方式一：脚本启动

前置条件：

- macOS
- 已安装并可运行 `openclaw`
- Python 3.9+

启动：

```bash
cd ~/openclaw-health-monitor
./install.sh
./start.sh
```

常用命令：

```bash
cd ~/openclaw-health-monitor
./start.sh
./status.sh
./verify.sh
./stop.sh
```

官方最新版并行验证：

```bash
cd ~/openclaw-health-monitor
./manage_official_openclaw.sh prepare
./manage_official_openclaw.sh start
./manage_official_openclaw.sh status
./manage_official_openclaw.sh stop
```

### 方式二：桌面 App

直接下载：

- [下载 dmg](https://github.com/DreamOfXM/openclaw-health-monitor/releases/latest/download/openclaw-health-monitor-macos-arm64.dmg)
- [下载 app zip](https://github.com/DreamOfXM/openclaw-health-monitor/releases/latest/download/openclaw-health-monitor-macos-arm64.app.zip)

桌面 App 行为：

- 打开 App：自动拉起 Gateway、Guardian、Dashboard
- 退出 App：停止 Gateway、Guardian、Dashboard

当前桌面 App 仍然依赖本机已准备好 `~/openclaw-health-monitor` 仓库和运行环境。

## 这个项目是干什么的

可以把它理解成 OpenClaw Gateway 的本地值班台：

- `Gateway`
  真正提供 OpenClaw 能力的核心服务
- `Guardian`
  后台守护进程，负责健康检查、异常识别、告警、主动进度播报和受控恢复
- `Dashboard`
  本地网页控制台，负责展示状态、问题定位、错误日志和操作入口

如果你只关心“怎么用”，记住这四个命令就够了：

```bash
./install.sh
./start.sh
./status.sh
./stop.sh
```

如果你要在不动当前工作版本的前提下验证 OpenClaw 官方最新版，再记住这一组：

```bash
./manage_official_openclaw.sh prepare
./manage_official_openclaw.sh start
./manage_official_openclaw.sh status
./manage_official_openclaw.sh stop
```

## 架构说明

### 核心组件

- `guardian.py`
  后台守护进程。负责健康检查、异常识别、主动进度播报、自动恢复、通知和变更记录。

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
   持续轮询 Gateway 和运行日志，识别长时间无回复、阶段停滞、网关异常，并在长任务场景下主动推送进度或升级播报

4. `Dashboard`
   提供本地问题定位面板、最近异常、内存归因、快照操作，以及多版本 OpenClaw 环境管理

5. `./stop.sh`
   调用 `desktop_runtime.sh stop all`，停止整套本地运行面

### 官方最新版并行验证

Health Monitor 现在还可以托管一套“官方最新版 OpenClaw”的隔离验证环境，不需要直接改你当前在用的 OpenClaw 仓库，也不需要先切换生产环境。

默认路径和端口：

- 官方 worktree：
  - `~/openclaw-workspace/openclaw-official`
- 官方隔离状态目录：
  - `~/.openclaw-official`
- 官方验证端口：
  - `19001`

这套验证环境会：

- 从你当前 OpenClaw 仓库拉出一个 `origin/main` worktree
- 同步一份隔离的私有配置和 workspace
- 在隔离目录中安装依赖并构建
- 用独立端口拉起验证 Gateway

核心命令：

```bash
cd ~/openclaw-health-monitor
./manage_official_openclaw.sh prepare
./manage_official_openclaw.sh start
./manage_official_openclaw.sh status
./manage_official_openclaw.sh stop
```

也支持 Makefile 入口：

```bash
make official-prepare
make official-start
make official-status
make official-stop
```

### OpenClaw 自动更新

这个项目现在支持“由守护助手托管 OpenClaw 官方最新版的更新”，不需要改 OpenClaw 源码。

更新策略：

- 每次更新都会先刷新官方 worktree
- 再同步隔离配置
- 然后重新安装依赖并构建
- 默认不直接替换你当前正在用的工作版本

安装定时更新：

```bash
cd ~/openclaw-health-monitor
./manage_official_openclaw.sh install-schedule
./manage_official_openclaw.sh schedule-status
```

默认会安装一个 `launchd` 任务：

- Label:
  - `ai.openclaw.official-update`
- 默认时间：
  - 每天 `04:30`

这套自动更新默认只更新：

- `~/openclaw-workspace/openclaw-official`
- `~/.openclaw-official`

不会直接覆盖你当前主用的：

- `~/openclaw-workspace/openclaw`
- `~/.openclaw`

### 版本环境管理面板

Dashboard 首页现在会直接展示：

- 当前主用版
- 官方验证版
- 当前守护目标环境
- 每个环境各自的端口、版本、健康状态和 Dashboard 链接

你可以直接在页面里做两件事：

- 查看当前守护的是哪一套 OpenClaw
- 一键切换守护目标环境

切换行为默认是互斥的：

- 切到官方验证版时，会停掉当前主用版 Gateway
- 切回当前主用版时，会停掉官方验证版 Gateway

这样不会出现两套 OpenClaw 同时抢同一套通道的情况。

注意：

- 如果你通过 Dashboard 页面切换环境，`ACTIVE_OPENCLAW_ENV` 和本地 SQLite 状态都会同步更新
- 如果你绕开 Health Monitor，直接手工执行 OpenClaw 自己的脚本、`launchd`、或其他外部命令去启动/停止 Gateway，本地数据库里的“当前活动环境”记录不一定同步变化
- 这时页面展示可能会短暂落后于真实进程状态，建议重新通过 Dashboard 切换一次，或手动同步配置后再继续使用

## 运行验证

完成安装或升级后，可按下面顺序验证本地监控是否正常工作。

### 1. 基础启动验证

```bash
cd ~/openclaw-health-monitor
./preflight.sh
./start.sh
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

Guardian 同时支持单 agent / 多 agent：

- 单 agent 场景：
  - 长时间无最终回复会被识别为 `dispatch_stuck`
  - 没有可见回复会被识别为 `no_reply`
- 多 agent 场景：
  - 会继续识别 `PIPELINE_PROGRESS` 阶段是否长时间无推进
  - 并可对长任务主动做进度推送和升级播报

### 2.1 轮询与主动进度推送

Guardian 不是被动看板，而是会按固定间隔轮询运行状态和运行日志。

默认相关配置：

- `CHECK_INTERVAL`
  - 轮询检查间隔
- `SLOW_RESPONSE_THRESHOLD`
  - 慢响应阈值
- `STALLED_RESPONSE_THRESHOLD`
  - 无回复 / 卡住阈值
- `PROGRESS_PUSH_INTERVAL`
  - 长时间无新进展后的首次主动推送阈值
- `PROGRESS_PUSH_COOLDOWN`
  - 两次主动进度推送之间的冷却时间
- `PROGRESS_ESCALATION_INTERVAL`
  - 长时间无新进展后的升级播报阈值
- `GUARDIAN_FOLLOWUP_TIMEOUT / GUARDIAN_FOLLOWUP_RETRIES / GUARDIAN_FOLLOWUP_RETRY_DELAY`
  - 会话内守护追问的超时、重试和降级兜底配置

设计目标：

- 用户不用反复追问“现在做得怎么样了”
- Guardian 先看运行日志里是否真的长期没有新进展，而不是机械按短周期刷屏
- 对长时间无新进展的任务，Guardian 会优先在原会话里发带标记的系统追问；如果会话追问超时，会自动降级为直接进度推送
- 如果静默时间继续拉长，Guardian 会升级播报，而不是静默等待

### 3. 内存归因验证

首页内存区会明确显示：

- `Top 15 进程`
- `Kernel / Wired`
- `Compressed`
- `Other System`

也会直接告诉你：

- `Top 15` 覆盖了多少已用内存
- 还有多少属于系统/缓存/未归属项

### 4. 通知验证

如果已经配置钉钉或飞书 webhook，检查：

- 异常首次出现时会发送通知
- 同类异常在去重窗口内不会刷屏

### 5. 快速回归验证

```bash
python3 -m unittest discover -s tests
```

在线验收可直接运行：

```bash
cd ~/openclaw-health-monitor
./verify.sh
```

## GitHub Actions

仓库已经提供 macOS 构建 workflow：

- `.github/workflows/release.yml`
- `.github/release.yml`

它会自动完成：

- 安装 Python 依赖
- 安装 `pnpm`
- 安装 Rust toolchain
- 运行测试
- 构建桌面 App
- 整理 `.dmg` 和 `.app.zip`
- 上传为 workflow artifacts

当仓库 push `v*` tag 时，workflow 会把 `release/` 里的文件自动附加到 GitHub Release。

推荐发布步骤：

```bash
cd ~/openclaw-health-monitor
make test
make pake
make release
```

## English

OpenClaw Health Monitor is a local monitoring, diagnosis, and recovery console for OpenClaw Gateway.
It runs three parts together as a local control plane:

- `Gateway`: the core OpenClaw runtime
- `Guardian`: the background watcher for health checks, anomaly detection, alerts, and controlled recovery
- `Dashboard`: the local control plane UI for status, logs, and operator actions

It is designed for two groups:

- non-technical users who want a simple start / stop / status workflow
- technical users who want to inspect runtime health, recovery behavior, recent anomalies, and memory attribution

## Quick Start

### Option 1: Script Startup

Requirements:

- macOS
- a working `openclaw` command
- Python 3.9+

Start the full stack:

```bash
cd ~/openclaw-health-monitor
./install.sh
./start.sh
```

Common commands:

```bash
cd ~/openclaw-health-monitor
./start.sh
./status.sh
./verify.sh
./stop.sh
```

### Option 2: Desktop App

Direct downloads:

- [Download dmg](https://github.com/DreamOfXM/openclaw-health-monitor/releases/latest/download/openclaw-health-monitor-macos-arm64.dmg)
- [Download app zip](https://github.com/DreamOfXM/openclaw-health-monitor/releases/latest/download/openclaw-health-monitor-macos-arm64.app.zip)

Desktop app behavior:

- open the app: automatically start Gateway, Guardian, and Dashboard
- quit the app: stop Gateway, Guardian, and Dashboard

The current desktop app still assumes the local repository and runtime environment already exist at `~/openclaw-health-monitor`.

## What This Project Does

You can think of this project as the local operator console for OpenClaw Gateway:

- `Gateway`
  the service that actually does the work
- `Guardian`
  the watcher that checks health, detects anomalies, records incidents, and performs controlled recovery
- `Dashboard`
  the UI that shows current status, problem focus, recent incidents, and operator actions

If you only care about daily usage, these are the four commands that matter most:

```bash
./install.sh
./start.sh
./status.sh
./verify.sh
./stop.sh
```

## Architecture

### Core Components

- `guardian.py`
  Background daemon responsible for health checks, anomaly detection, recovery logic, notifications, and change logs.

- `dashboard.py`
  Local web UI that renders Gateway / Guardian / Dashboard state, recent incidents, memory attribution, snapshots, and operator controls.

- `desktop_runtime.sh`
  The local runtime controller that starts, stops, and inspects:
  - Gateway
  - Guardian
  - Dashboard

- `monitor_config.py`
  Config loader with support for:
  - `config.conf`
  - `config.local.conf`
  - environment variable overrides

- `state_store.py`
  SQLite-backed local state storage for:
  - alerts
  - versions
  - change events
  - health samples

### Runtime Model

1. `./start.sh`
   calls `desktop_runtime.sh start all`

2. `desktop_runtime.sh`
   starts Gateway, Guardian, and Dashboard in order and records PID files

3. `Guardian`
   keeps checking Gateway health, scans runtime anomalies, records changes, and sends notifications

4. `Dashboard`
   exposes the local problem-focus view, recent anomalies, memory attribution, and snapshot actions

5. `./stop.sh`
   calls `desktop_runtime.sh stop all` and stops the whole local stack

### Version Environment Panel

The Dashboard homepage now shows:

- the current primary environment
- the official validation environment
- the currently guarded target environment
- each environment's port, version, health status, and Dashboard link

You can use it to:

- see which OpenClaw environment Guardian is currently managing
- switch the guarded target with one click

Switching is mutually exclusive by default:

- switching to the official validation environment stops the primary Gateway
- switching back to the primary environment stops the official validation Gateway

This avoids two OpenClaw instances competing for the same channels.

Important:

- when you switch environments through the Dashboard, both `ACTIVE_OPENCLAW_ENV` and the local SQLite state are updated together
- if you bypass Health Monitor and start or stop Gateway manually through raw scripts, `launchd`, or other external commands, the local database may not immediately reflect the real active environment
- in that case the UI can temporarily lag behind the actual process state; switch once again through the Dashboard or resync the config before relying on the panel

## Validation

After installation or upgrade, validate the local monitor in this order.

### 1. Basic Startup

```bash
cd ~/openclaw-health-monitor
./preflight.sh
./start.sh
```

Check that:

- the Dashboard loads correctly
- both `Guardian` and `Gateway` are visible
- the recent incidents section and problem-focus section render without frontend errors

### 2. Anomaly Detection

These runtime situations should become visible in the change log and the incident area:

- `dispatch complete (queuedFinal=false, replies=0)` becomes "completed without visible reply"
- `gateway closed (1006 ...)` becomes `gateway_ws_closed`
- `abort failed ... no_active_run` becomes a run-tracking anomaly
- long-running `dispatching to agent` without a final completion becomes "stuck without final result"
- being stuck in a single `PIPELINE_PROGRESS` stage becomes a stage-stuck anomaly

### 3. Memory Attribution

The homepage memory section should make memory usage explainable:

- `Top 15 Processes`
- `Kernel / Wired`
- `Compressed`
- `Other System`

It should also explain:

- how much of used memory is covered by the Top 15 processes
- how much remains in system/cache/unattributed memory

### 4. Notifications

If DingTalk or Feishu webhooks are configured, verify:

- the first occurrence of an anomaly triggers a notification
- repeated anomalies are deduplicated within the configured interval

### 5. Quick Regression

Run the local test suite:

```bash
python3 -m unittest discover -s tests
```

Run the online verification script:

```bash
cd ~/openclaw-health-monitor
./verify.sh
```

## GitHub Actions

The repository already includes a macOS release workflow:

- `.github/workflows/release.yml`
- `.github/release.yml`

It automatically handles:

- Python dependency installation
- `pnpm` setup
- Rust toolchain setup
- test execution
- desktop app build
- `.dmg` and `.app.zip` packaging
- workflow artifact upload

When a `v*` tag is pushed, the workflow attaches the generated files to the GitHub Release.

Recommended release flow:

```bash
cd ~/openclaw-health-monitor
make test
make pake
make release
```
