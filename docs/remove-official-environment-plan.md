# 移除 Official 环境计划

## 目标

将 Health Monitor 从双环境（primary + official）简化为单环境（primary only）。

## 涉及的文件

### 需要删除的文件

| 文件 | 说明 |
|------|------|
| `manage_official_openclaw.sh` | Official 环境管理脚本 |
| `promotion_controller.py` | Official -> Primary 升级控制器 |
| `tests/test_promotion_controller.py` | 升级控制器测试 |
| `snapshots/*-official/` | Official 环境快照 |

### 需要修改的文件

| 文件 | 修改内容 |
|------|----------|
| `config.conf` | 移除 OPENCLAW_OFFICIAL_* 配置 |
| `monitor_config.py` | 移除 official 相关配置和函数 |
| `desktop_runtime.sh` | 移除 official 环境处理逻辑 |
| `guardian.py` | 移除 official 环境处理逻辑 |
| `dashboard_backend.py` | 移除 official 环境相关 API 和 UI |
| `dashboard_v2/routes/environments.py` | 移除 official 环境处理 |
| `dashboard_v2/services/data_collector.py` | 移除 official 环境数据收集 |
| `docs/*.md` | 更新文档，移除 official 相关内容 |

## 执行步骤

### Phase 1: 删除独立文件

1. 删除 `manage_official_openclaw.sh`
2. 删除 `promotion_controller.py`
3. 删除 `tests/test_promotion_controller.py`
4. 删除 official 相关快照

### Phase 2: 修改配置文件

1. 修改 `config.conf` - 移除 OPENCLAW_OFFICIAL_* 配置
2. 修改 `monitor_config.py` - 移除 official 相关配置

### Phase 3: 修改核心代码

1. 修改 `desktop_runtime.sh` - 移除 official 环境处理逻辑
2. 修改 `guardian.py` - 移除 official 环境处理逻辑
3. 修改 `dashboard_backend.py` - 移除 official 环境相关 API

### Phase 4: 更新文档

1. 更新 `docs/architecture.md`
2. 更新 `docs/product-architecture.md`
3. 更新其他相关文档

### Phase 5: 测试验证

1. 运行测试确保没有破坏现有功能
2. 手动验证 Dashboard 正常工作
3. 验证 Guardian 正常工作

## 风险评估

| 风险 | 影响 | 缓解措施 |
|------|------|----------|
| 删除代码导致功能缺失 | 中 | 仔细检查所有引用 |
| 配置不兼容 | 低 | 保留向后兼容的默认值 |
| 测试失败 | 低 | 更新测试用例 |

## 预期结果

- 代码量减少约 20%
- 配置简化
- 维护成本降低
- 不再有环境切换问题