/**
 * 管理视图 JavaScript
 */

document.addEventListener('DOMContentLoaded', () => {
    initManage();
});

let currentConfirmAction = null;
let confirmActionInFlight = false;
let manageEnvironmentData = null;
let manageHealthScoreData = null;
let manageTaskData = null;

function initManage() {
    loadManageData();

    document.getElementById('btn-promote')?.addEventListener('click', () => {
        showConfirmModal('promote');
    });
    document.getElementById('btn-create-snapshot')?.addEventListener('click', createSnapshot);
    document.getElementById('btn-switch-official')?.addEventListener('click', () => {
        showConfirmModal('switch', { target: 'official' });
    });
    document.getElementById('btn-switch-primary')?.addEventListener('click', () => {
        showConfirmModal('switch', { target: 'primary' });
    });
    document.getElementById('btn-enable-official-auto-update')?.addEventListener('click', () => {
        toggleOfficialAutoUpdate(true);
    });
    document.getElementById('btn-disable-official-auto-update')?.addEventListener('click', () => {
        toggleOfficialAutoUpdate(false);
    });
    document.getElementById('btn-save-config')?.addEventListener('click', saveConfig);
    document.getElementById('btn-confirm-cancel')?.addEventListener('click', hideConfirmModal);
    document.getElementById('btn-confirm-ok')?.addEventListener('click', executeConfirmedAction);
    document.querySelector('.modal-overlay')?.addEventListener('click', hideConfirmModal);
    document.querySelector('.modal-close')?.addEventListener('click', hideConfirmModal);
}

async function loadManageData() {
    try {
        const envResponse = await API.getEnvironment(true);
        manageEnvironmentData = envResponse.success ? envResponse.data : null;
    } catch (error) {
        console.error('加载管理页主数据失败:', error);
    }

    await Promise.all([
        loadPromotionStatus(),
        loadSnapshots(),
        loadConfig(),
        loadBindingAlerts()
    ]);

    void refreshManageSecondaryData();
}

async function refreshManageSecondaryData() {
    try {
        const [healthResponse, tasksResponse] = await Promise.all([
            API.getHealthScore(false),
            API.getTasks(false)
        ]);
        manageHealthScoreData = healthResponse.success ? healthResponse.data : null;
        manageTaskData = tasksResponse.success ? tasksResponse.data : null;
        await loadPromotionStatus();
    } catch (error) {
        console.error('加载管理页次级数据失败:', error);
    }
}

async function loadBindingAlerts() {
    try {
        const envData = manageEnvironmentData || {};
        const integrity = envData.environment_integrity || [];
        const bindingAudit = envData.binding_audit || {};
        const summaryEl = document.getElementById('binding-audit-summary');
        const container = document.getElementById('binding-alerts-container');
        if (summaryEl) {
            const activeEnv = (envData.active_environment || '--').toUpperCase();
            const boundEnv = (bindingAudit.active_env || envData.active_environment || '--').toUpperCase();
            const switchState = bindingAudit.switch_state || 'unknown';
            summaryEl.textContent = `当前激活: ${activeEnv} | DB绑定: ${boundEnv} | switch_state: ${switchState}`;
        }
        if (!container) return;
        if (!integrity.length) {
            container.innerHTML = '<div class="empty">未发现绑定漂移或 listener 异常</div>';
            return;
        }
        container.innerHTML = integrity.map(issue => `
            <div class="snapshot-item">
                <div class="snapshot-info">
                    <span class="snapshot-name">${issue.title || issue.code || '绑定异常'}</span>
                    <span class="snapshot-date">${issue.severity === 'error' ? 'ERROR' : 'WARN'}</span>
                </div>
                <div class="env-status-text">${issue.detail || ''}</div>
            </div>
        `).join('');
    } catch (error) {
        console.error('加载绑定巡检失败:', error);
    }
}

async function loadPromotionStatus() {
    try {
        const envData = manageEnvironmentData || {};
        const healthData = manageHealthScoreData || { score: 0 };
        const taskData = manageTaskData || { blocked_count: 0 };
        if (!envData || !Object.keys(envData).length) return;
        const promotionSummary = envData.promotion_summary || {};

        const officialEnv = (envData.environments || []).find(item => item.id === 'official') || {};
        const activeEnv = envData.active_environment || '';

        // 首先检查 Official 是否在运行
        const isOfficialRunning = Boolean(officialEnv.running && officialEnv.healthy);
        
        let checks = [];
        let allPassed = false;
        
        if (!isOfficialRunning) {
            // Official 未运行，只显示这一条
            checks = [
                {
                    name: 'Official 环境运行正常',
                    passed: false,
                    message: activeEnv === 'primary'
                        ? '当前运行的是 Primary，Official 作为验证环境尚未启动'
                        : '官方验证版未运行，请先启动 Official 环境'
                }
            ];
            allPassed = false;
        } else {
            // Official 在运行，检查所有条件
            const healthReady = Boolean(manageHealthScoreData);
            const taskReady = Boolean(manageTaskData);
            checks = [
                {
                    name: 'Official 环境运行正常',
                    passed: true
                },
                {
                    name: 'Control UI 可用',
                    passed: Boolean(officialEnv.control_ui_ready)
                },
                {
                    name: '健康评分 > 80',
                    passed: healthReady ? Number(healthData.score || 0) >= 80 : false,
                    message: healthReady ? '' : '正在加载评分'
                },
                {
                    name: '无阻塞任务',
                    passed: taskReady ? Number(taskData.blocked_count || 0) === 0 : false,
                    message: taskReady ? '' : '正在加载任务状态'
                }
            ];
            allPassed = healthReady && taskReady && checks.every(check => check.passed) && Boolean(promotionSummary.safe_to_promote);
        }

        const promotionStatusEl = document.getElementById('promotion-status');
        const promotionReadyEl = document.getElementById('promotion-ready');
        if (promotionStatusEl) {
            promotionStatusEl.textContent = allPassed ? '就绪' : '未就绪';
            promotionStatusEl.className = 'status-value ' + (allPassed ? 'ready' : 'not-ready');
        }
        if (promotionReadyEl) {
            let headline;
            if (!isOfficialRunning) {
                headline = activeEnv === 'primary'
                    ? 'ℹ️ Official 验证环境未启动'
                    : '❌ Official 环境未运行';
            } else {
                headline = promotionSummary.headline || (allPassed ? '✅ 可以晋升' : '❌ 条件不满足');
            }
            promotionReadyEl.textContent = headline;
        }

        const checklistEl = document.getElementById('promotion-checks');
        if (checklistEl) {
            checklistEl.innerHTML = checks.map(check => {
                let icon, statusClass;
                if (check.passed) {
                    icon = '✅';
                    statusClass = 'passed';
                } else if (check.name === 'Official 环境运行正常' && !isOfficialRunning) {
                    icon = '❌';
                    statusClass = 'failed';
                } else {
                    icon = '⏳';
                    statusClass = 'pending';
                }
                const message = check.message ? ` - ${check.message}` : '';
                return `
                    <li class="check-item ${statusClass}">
                        <span class="check-icon">${icon}</span>
                        <span class="check-text">${check.name}${message}</span>
                    </li>
                `;
            }).join('');
        }

        const promoteBtn = document.getElementById('btn-promote');
        if (promoteBtn) {
            if (!isOfficialRunning) {
                promoteBtn.disabled = true;
                promoteBtn.title = 'Official 环境未运行，无法晋升';
            } else {
                promoteBtn.disabled = false;
                promoteBtn.title = allPassed ? '已满足建议晋升条件' : '当前存在风险，但仍允许手动执行晋升';
            }
        }

        const currentEnvName = document.getElementById('current-env-name');
        const currentEnvStatus = document.getElementById('current-env-status');
        if (currentEnvName) {
            currentEnvName.textContent = (envData.active_environment || '--').toUpperCase();
        }
        if (currentEnvStatus) {
            currentEnvStatus.textContent = !isOfficialRunning && activeEnv === 'primary'
                ? '当前主用版正在运行；如需走验证晋升，请先启动 Official 环境。'
                : (promotionSummary.recommended_action || '当前激活环境');
        }

        const activeBadge = document.getElementById('active-env');
        if (activeBadge) {
            activeBadge.textContent = (envData.active_environment || '--').toUpperCase();
        }

        // 更新环境切换按钮高亮状态
        updateSwitchButtons(envData.active_environment, envData.environments || []);
        updateOfficialAutoUpdate(envData.environments || []);
    } catch (error) {
        console.error('加载晋升状态失败:', error);
    }
}

function updateOfficialAutoUpdate(environments = []) {
    const official = environments.find(item => item.id === 'official') || {};
    const statusEl = document.getElementById('official-auto-update-status');
    const detailEl = document.getElementById('official-auto-update-detail');
    const enableBtn = document.getElementById('btn-enable-official-auto-update');
    const disableBtn = document.getElementById('btn-disable-official-auto-update');
    const enabled = Boolean(official.auto_update_enabled);
    const expected = Boolean(official.auto_update_expected);
    const installed = Boolean(official.auto_update_installed);
    const drift = Boolean(official.auto_update_drift);
    if (statusEl) {
        statusEl.textContent = enabled ? '已启用' : '已关闭';
    }
    if (detailEl) {
        detailEl.textContent = `配置=${expected ? '开启' : '关闭'} | 调度器=${installed ? '已安装' : '未安装'}${drift ? ' | 存在漂移' : ''}`;
    }
    if (enableBtn) enableBtn.disabled = enabled;
    if (disableBtn) disableBtn.disabled = !enabled;
}

async function toggleOfficialAutoUpdate(enabled) {
    try {
        const response = await API.setOfficialAutoUpdate(enabled);
        if (!response.success) {
            throw new Error(response.error || response.data?.message || '切换官方自动更新失败');
        }
        showToast(response.data.message || (enabled ? '已启用官方自动更新' : '已关闭官方自动更新'), 'success');
        await loadManageData();
    } catch (error) {
        console.error('切换官方自动更新失败:', error);
        showToast('切换官方自动更新失败: ' + error.message, 'error');
    }
}

/**
 * 更新环境切换按钮高亮状态
 */
function updateSwitchButtons(activeEnv, environments = []) {
    const btnOfficial = document.getElementById('btn-switch-official');
    const btnPrimary = document.getElementById('btn-switch-primary');
    const consoleLink = document.getElementById('console-dashboard-link');
    const consoleMsg = document.getElementById('console-inactive-msg');
    
    if (!btnOfficial || !btnPrimary) return;
    
    // 重置按钮状态
    btnOfficial.className = 'btn btn-secondary btn-switch';
    btnPrimary.className = 'btn btn-secondary btn-switch';
    btnOfficial.textContent = '切换到 Official';
    btnPrimary.textContent = '切换到 Primary';
    
    // 高亮当前环境按钮
    if (activeEnv === 'official') {
        btnOfficial.classList.add('active');
        btnOfficial.classList.remove('btn-secondary');
        btnOfficial.classList.add('btn-success');
        btnOfficial.textContent = '✅ 当前环境 (Official)';
        btnOfficial.disabled = true;
        btnPrimary.disabled = false;
    } else if (activeEnv === 'primary') {
        btnPrimary.classList.add('active');
        btnPrimary.classList.remove('btn-secondary');
        btnPrimary.classList.add('btn-success');
        btnPrimary.textContent = '✅ 当前环境 (Primary)';
        btnPrimary.disabled = true;
        btnOfficial.disabled = false;
    }
    
    // 更新控制台链接状态
    if (consoleLink && consoleMsg) {
        const activeEnvData = environments.find(item => item.id === activeEnv) || {};
        const dashboardLink = activeEnvData.dashboard_url || activeEnvData.dashboard_open_link || '';
        const canOpenDashboard = Boolean(activeEnvData.running && dashboardLink);

        if (canOpenDashboard) {
            consoleLink.classList.remove('disabled');
            consoleLink.href = dashboardLink;
            consoleMsg.style.display = 'none';
        } else {
            consoleLink.classList.add('disabled');
            consoleLink.href = '#';
            consoleMsg.style.display = 'block';
            if (!activeEnvData.running) {
                consoleMsg.textContent = `${(activeEnv || '--').toUpperCase()} 环境未运行，无法访问控制台`;
            } else {
                consoleMsg.textContent = `${(activeEnv || '--').toUpperCase()} 环境当前不可打开控制台`;
            }
        }
    }
}

async function loadSnapshots() {
    try {
        const response = await API.getSnapshots(true);
        if (!response.success) return;
        updateSnapshotsList(response.data.snapshots || []);
    } catch (error) {
        console.error('加载快照失败:', error);
    }
}

function updateSnapshotsList(snapshots) {
    const container = document.getElementById('snapshots-container');
    if (!container) return;

    if (snapshots.length === 0) {
        container.innerHTML = '<div class="empty">暂无快照</div>';
        return;
    }

    container.innerHTML = snapshots.map(snapshot => `
        <div class="snapshot-item">
            <div class="snapshot-info">
                <span class="snapshot-name">${snapshot.name}</span>
                <span class="snapshot-date">${UI.formatDateTime(snapshot.created_at)}</span>
            </div>
            <div class="snapshot-actions">
                <button class="btn btn-small" onclick="restoreSnapshot('${snapshot.name}')">恢复</button>
            </div>
        </div>
    `).join('');
}

async function createSnapshot() {
    const labelInput = document.getElementById('snapshot-label');
    const label = labelInput?.value?.trim();
    if (!label) {
        showToast('请输入快照标签', 'error');
        return;
    }

    try {
        const response = await API.createSnapshot(label);
        if (!response.success) {
            throw new Error(response.error || '创建快照失败');
        }
        showToast(`快照创建成功，共生成 ${response.data.count} 个快照`, 'success');
        if (labelInput) labelInput.value = '';
        await loadSnapshots();
    } catch (error) {
        console.error('创建快照失败:', error);
        showToast('创建快照失败: ' + error.message, 'error');
    }
}

async function restoreSnapshot(snapshotName) {
    if (!confirm(`确定要恢复快照 ${snapshotName} 吗？此操作不可逆。`)) {
        return;
    }

    try {
        const response = await API.restoreSnapshot(snapshotName);
        if (!response.success) {
            throw new Error(response.error || response.data?.message || '恢复失败');
        }
        showToast(response.data.message || `快照 ${snapshotName} 恢复成功`, 'success');
        await loadManageData();
    } catch (error) {
        console.error('恢复快照失败:', error);
        showToast('恢复快照失败: ' + error.message, 'error');
    }
}

function showConfirmModal(action, params = {}) {
    currentConfirmAction = { action, params };
    const modal = document.getElementById('confirm-modal');
    const messageEl = document.getElementById('confirm-message');
    const codeSection = document.querySelector('.confirm-input');

    if (!modal || !messageEl) return;

    switch (action) {
        case 'promote':
            messageEl.textContent = '您确定要执行版本晋升吗？此操作将把 Official 提升为 Primary。';
            break;
        case 'switch':
            messageEl.textContent = `您确定要切换到 ${params.target?.toUpperCase()} 环境吗？此操作会切换当前激活环境。`;
            break;
    }

    // 隐藏验证码输入部分
    if (codeSection) {
        codeSection.style.display = 'none';
    }

    modal.classList.add('active');
}

function hideConfirmModal(force = false) {
    if (confirmActionInFlight && !force) return;
    const modal = document.getElementById('confirm-modal');
    if (modal) {
        modal.classList.remove('active');
    }
    currentConfirmAction = null;
}

async function executeConfirmedAction() {
    if (confirmActionInFlight) return;
    console.log('执行确认操作，currentConfirmAction:', currentConfirmAction);
    
    if (!currentConfirmAction) {
        console.error('currentConfirmAction 为空');
        showToast('操作信息丢失，请重试', 'error');
        return;
    }

    setConfirmActionLoading(true);
    try {
        switch (currentConfirmAction.action) {
            case 'promote':
                await executePromotion();
                break;
            case 'switch':
                await executeSwitch(currentConfirmAction.params.target);
                break;
        }
        setConfirmActionLoading(false);
        hideConfirmModal(true);
    } catch (error) {
        console.error('执行操作失败:', error);
        showToast('操作失败: ' + error.message, 'error');
    } finally {
        if (confirmActionInFlight) {
            setConfirmActionLoading(false);
        }
    }
}

async function executePromotion() {
    const response = await API.promoteEnvironment('PROMOTE');
    if (!response.success) {
        throw new Error(response.error || response.data?.message || '晋升失败');
    }
    showToast(response.data.message || '版本晋升已执行', 'success');
    await loadManageData();
}

async function executeSwitch(targetEnv) {
    const response = await API.switchEnvironment(targetEnv);
    if (!response.success) {
        throw new Error(response.error || response.data?.message || '切换失败');
    }
    showToast(response.data.message || `已切换到 ${targetEnv.toUpperCase()} 环境`, 'success');
    setTimeout(() => window.location.reload(), 500);
}

async function loadConfig() {
    try {
        const response = await API.getEnvironment();
        if (!response.success) return;
        const envData = response.data;
        const readiness = envData.context_readiness || {};
        const drift = envData.config_drift || {};

        const refreshInput = document.getElementById('config-refresh');
        const webhookInput = document.getElementById('config-webhook');
        const loglevelSelect = document.getElementById('config-loglevel');

        if (refreshInput) {
            refreshInput.value = 5;
        }
        if (webhookInput) {
            webhookInput.value = `${readiness.status || 'unknown'} / ${drift.status || 'unknown'}`;
            webhookInput.readOnly = true;
        }
        if (loglevelSelect) {
            loglevelSelect.value = 'info';
        }
    } catch (error) {
        console.error('加载配置失败:', error);
    }
}

async function saveConfig() {
    const refreshInput = document.getElementById('config-refresh');
    const loglevelSelect = document.getElementById('config-loglevel');
    if (refreshInput) {
        AppState.refreshIntervals.healthScore = parseInt(refreshInput.value || '5', 10) * 1000;
        AppState.refreshIntervals.metrics = AppState.refreshIntervals.healthScore;
        AppState.refreshIntervals.events = AppState.refreshIntervals.healthScore;
        AppState.refreshIntervals.environment = Math.max(AppState.refreshIntervals.healthScore, 10000);
    }
    if (loglevelSelect) {
        localStorage.setItem('dashboard_v2_log_level', loglevelSelect.value);
    }
    showToast('面板本地配置已保存。业务配置仍以原系统配置为准。', 'success');
}

function setConfirmActionLoading(loading) {
    confirmActionInFlight = loading;
    const confirmBtn = document.getElementById('btn-confirm-ok');
    const cancelBtn = document.getElementById('btn-confirm-cancel');
    const closeBtn = document.querySelector('.modal-close');
    if (confirmBtn) {
        confirmBtn.disabled = loading;
        confirmBtn.textContent = loading ? '执行中...' : '确认执行';
    }
    if (cancelBtn) {
        cancelBtn.disabled = loading;
    }
    if (closeBtn) {
        closeBtn.disabled = loading;
    }
}

function showToast(message, type = 'info') {
    const toast = document.createElement('div');
    toast.className = `manage-toast manage-toast-${type}`;
    toast.textContent = message;
    toast.style.cssText = `
        position: fixed;
        right: 24px;
        top: 24px;
        z-index: 4000;
        min-width: 260px;
        max-width: 420px;
        padding: 12px 16px;
        border-radius: 12px;
        color: #fff;
        font-size: 14px;
        line-height: 1.5;
        box-shadow: 0 12px 30px rgba(15, 23, 42, 0.28);
        background: ${type === 'error' ? 'linear-gradient(135deg, #b42318, #7a1a14)' : 'linear-gradient(135deg, #1769aa, #0f4c81)'};
        animation: manageToastSlideIn 0.24s ease;
    `;

    if (!document.getElementById('manage-toast-style')) {
        const style = document.createElement('style');
        style.id = 'manage-toast-style';
        style.textContent = `
            @keyframes manageToastSlideIn {
                from { transform: translateX(24px); opacity: 0; }
                to { transform: translateX(0); opacity: 1; }
            }
            @keyframes manageToastSlideOut {
                from { transform: translateX(0); opacity: 1; }
                to { transform: translateX(24px); opacity: 0; }
            }
        `;
        document.head.appendChild(style);
    }

    document.body.appendChild(toast);
    setTimeout(() => {
        toast.style.animation = 'manageToastSlideOut 0.24s ease forwards';
        setTimeout(() => toast.remove(), 240);
    }, 2200);
}
