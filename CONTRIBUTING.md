# Contributing / 贡献指南

## English

Thanks for contributing to OpenClaw Health Monitor.

Before sending changes:

1. Keep runtime secrets out of tracked files.
2. Put machine-local values in `config.local.conf`.
3. Run tests:

```bash
cd ~/openclaw-health-monitor
.venv/bin/python -m pytest dashboard_v2/tests tests -q
```

4. Run release checks:

```bash
cd ~/openclaw-health-monitor
./prepare_release.sh
```

5. Do not commit:

- `config.local.conf`
- `snapshots/`
- `data/*.db`
- `data/*.db-shm`
- `data/*.db-wal`
- `data/current-task-facts.json`
- `data/task-registry-summary.json`
- `data/shared-state/*.json` (keep `data/shared-state/README.md` only)
- `.learnings/*.md`
- `MEMORY.md`
- `memory/*.md`
- `logs/`
- `change-logs/*.json`

6. Keep the console boundary clear:

- `dashboard_v2/` is the primary frontend
- `dashboard_backend.py` is the compatibility data layer
- do not add new primary UI work back into the old monolithic dashboard path

Recommended contribution scope:

- bug fixes
- safer recovery behavior
- better diagnostics
- installer and documentation improvements
- tests

Please keep changes small, reviewable, and reversible.

## 中文

欢迎为 OpenClaw Health Monitor 提交改进。

提交前请先确认：

1. 不要把运行期 secret 提交到受版本管理的文件。
2. 机器本地配置请放到 `config.local.conf`。
3. 先运行测试：

```bash
cd ~/openclaw-health-monitor
.venv/bin/python -m pytest dashboard_v2/tests tests -q
```

4. 再运行发布前检查：

```bash
cd ~/openclaw-health-monitor
./prepare_release.sh
```

5. 不要提交这些内容：

- `config.local.conf`
- `snapshots/`
- `data/*.db`
- `data/*.db-shm`
- `data/*.db-wal`
- `data/current-task-facts.json`
- `data/task-registry-summary.json`
- `data/shared-state/*.json`（保留 `data/shared-state/README.md`）
- `.learnings/*.md`
- `MEMORY.md`
- `memory/*.md`
- `logs/`
- `change-logs/*.json`

6. 保持控制台边界清晰：

- `dashboard_v2/` 是主前端
- `dashboard_backend.py` 是兼容数据层
- 不要再把新的主 UI 工作加回旧的 dashboard 路径里

推荐优先贡献：

- Bug 修复
- 更安全的恢复机制
- 更好的诊断与观测
- 安装器与文档改进
- 测试补充

请尽量保持改动小、易审查、可回滚。
