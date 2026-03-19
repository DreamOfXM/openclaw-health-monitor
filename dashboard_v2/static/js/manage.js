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
let snapshotPageOffset = 0;
const SNAPSHOT_PAGE_SIZE = 20;

function initManage() {
    loadManageData();

    document.getElementById('btn-restart-primary')?.addEventListener('click', () => {
        showConfirmModal('restart');
    });
    document.getElementById('btn-emergency-recover')?.addEventListener('click', executeEmergencyRecover);
    document.getElementById('btn-create-snapshot')?.addEventListener('click', createSnapshot);
    document.getElementById('btn-save-config')?.addEventListener('click', saveConfig);
    document.getElementById('btn-confirm-cancel')?.addEventListener('click', hideConfirmModal);
    document.getElementById('btn-confirm-ok')?.addEventListener('click', executeConfirmedAction);
    document.querySelector('.modal-overlay')?.addEventListener('click', hideConfirmModal);
    document.querySelector('.modal-close')?.addEventListener('click', hideConfirmModal);
}

async function loadManageData() {
    loadSnapshots(false, 0);

    try {
        const envResponse = await API.getEnvironment(true);
        manageEnvironmentData = envResponse.success ? envResponse.data : null;
    } catch (error) {
        console.error('加载管理页主数据失败:', error);
    }

    await Promise.all([loadRuntimeStatus(), loadConfig(), loadBindingAlerts()]);
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
        await loadRuntimeStatus();
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
            const switchState = bindingAudit.switch_state || 'unknown';
            summaryEl.textContent = `当前模式: 单环境 | 绑定状态: ${switchState}`;
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

async function loadRuntimeStatus() {
    try {
        const envData = manageEnvironmentData || {};
        const healthData = manageHealthScoreData || { score: 0 };
        const taskData = manageTaskData || { blocked_count: 0 };
        if (!envData || !Object.keys(envData).length) return;

        const activeEnv = envData.active_environment || 'primary';
        const primaryEnv = (envData.environments || []).find(item => item.id === 'primary') || {};
        const healthReady = Boolean(manageHealthScoreData);
        const taskReady = Boolean(manageTaskData);
        const checks = [
            {
                name: '运行环境运行正常',
                passed: Boolean(primaryEnv.running && primaryEnv.healthy),
                message: primaryEnv.running ? '' : 'OpenClaw Gateway 未运行'
            },
            {
                name: 'Control UI 可用',
                passed: Boolean(primaryEnv.control_ui_ready),
                message: primaryEnv.control_ui_ready ? '' : '控制台暂不可用'
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
        const allPassed = healthReady && taskReady && checks.every(check => check.passed);

        const currentEnvName = document.getElementById('current-env-name');
        const currentEnvStatus = document.getElementById('current-env-status');
        if (currentEnvName) {
            currentEnvName.textContent = 'OPENCLAW';
        }
        if (currentEnvStatus) {
            const readinessText = renderChannelReadinessSummary(primaryEnv);
            currentEnvStatus.textContent = allPassed
                ? 'OpenClaw 运行稳定，可继续服务。'
                : '当前存在运行风险，请优先处理检查清单中的未通过项。';
            if (readinessText) {
                currentEnvStatus.textContent += ` | ${readinessText}`;
            }
        }

        const activeBadge = document.getElementById('active-env');
        if (activeBadge) {
            activeBadge.textContent = 'OPENCLAW';
        }

        renderVersionRecovery(
            primaryEnv,
            envData.version_info || {},
            envData.recovery_profile || {},
            envData.watchdog_recovery_status || {},
            envData.watchdog_recovery_hints || [],
        );

        updateSwitchButtons(activeEnv, envData.environments || []);
    } catch (error) {
        console.error('加载运行状态失败:', error);
    }
}

function renderVersionRecovery(primaryEnv, versionInfo, recoveryProfile, watchdogStatus, watchdogHints) {
    const runtimeEl = document.getElementById('version-runtime-summary');
    const knownGoodEl = document.getElementById('version-known-good');
    const driftEl = document.getElementById('version-upstream-drift');
    const recoveryEl = document.getElementById('recovery-profile-summary');
    const watchdogSummaryEl = document.getElementById('watchdog-recovery-summary');
    const watchdogHintsEl = document.getElementById('watchdog-recovery-hints');
    const branch = versionInfo.branch || 'unknown';
    const describe = versionInfo.describe || primaryEnv.git_head || 'unknown';
    const shortCommit = versionInfo.short_commit || versionInfo.commit || 'unknown';
    if (runtimeEl) {
        runtimeEl.textContent = `${describe} · ${branch} · ${shortCommit}${versionInfo.dirty ? ' · dirty' : ''}`;
    }
    if (knownGoodEl) {
        const knownGood = recoveryProfile.known_good || {};
        knownGoodEl.textContent = `known good: ${knownGood.describe || knownGood.commit || '未记录'}`;
    }
    if (driftEl) {
        driftEl.textContent = `upstream 偏移: ahead ${Number(versionInfo.upstream_ahead || 0)} / behind ${Number(versionInfo.upstream_behind || 0)}`;
    }
    if (recoveryEl) {
        const hint = recoveryProfile.rollback_hint || {};
        recoveryEl.textContent = hint.target_describe || hint.target_commit
            ? `优先配置快照恢复；代码回退目标 ${hint.target_describe || hint.target_commit}`
            : '优先配置快照恢复；暂无 known good 代码回退目标';
    }
    if (watchdogSummaryEl) {
        watchdogSummaryEl.textContent = `watchdog: ${watchdogStatus.enabled === false ? 'disabled' : 'enabled'} | candidates ${Number(watchdogStatus.candidate_count || 0)} | dispatched ${Number(watchdogStatus.dispatched_count || 0)} | cooldown ${Number(watchdogStatus.cooldown_skips || 0)}`;
    }
    if (watchdogHintsEl) {
        const items = Array.isArray(watchdogHints) ? watchdogHints.slice(0, 5) : [];
        watchdogHintsEl.innerHTML = items.length
            ? items.map(item => `
                <div class="snapshot-item">
                    <div class="snapshot-info">
                        <span class="snapshot-name">${item.anomaly_type || 'watchdog_hint'}</span>
                        <span class="snapshot-date">${item.target_agent || 'main'}</span>
                    </div>
                    <div class="env-status-text">${item.hint_message || item.summary || ''}</div>
                </div>
            `).join('')
            : '<div class="empty">最近没有 watchdog 恢复提示</div>';
    }
}

function renderChannelReadinessSummary(env) {
    const readiness = env.channel_readiness || {};
    if (!readiness || !Object.keys(readiness).length) return '';
    const channelDetails = [];
    const structuredChannels = readiness.channels || {};
    for (const channel of Object.values(structuredChannels)) {
        if (channel && channel.name && channel.detail) {
            channelDetails.push(`${channel.name}=${channel.detail}`);
        }
    }
    if (!channelDetails.length) {
        const summary = String(readiness.summary || '');
        const pattern = /-\s+([A-Za-z]+)\s+default:\s+([^\n]+)/g;
        let match;
        while ((match = pattern.exec(summary)) !== null) {
            const channel = match[1];
            const detail = (match[2] || '').trim();
            if (channel && detail) {
                channelDetails.push(`${channel}=${detail}`);
            }
        }
    }
    return `通道检测=${readiness.status || 'unknown'}${channelDetails.length ? `，${channelDetails.join('；')}` : ''}`;
}

function updateSwitchButtons(activeEnv, environments = []) {
    const btnPrimary = document.getElementById('btn-restart-primary');
    const consoleLink = document.getElementById('console-dashboard-link');
    const consoleMsg = document.getElementById('console-inactive-msg');
    if (!btnPrimary) return;

    const primaryEnv = environments.find(item => item.id === 'primary') || {};
    btnPrimary.className = 'btn btn-secondary btn-switch';
    btnPrimary.textContent = primaryEnv.running ? '🔁 重启运行环境' : '启动运行环境';
    if (activeEnv === 'primary') {
        btnPrimary.classList.add('active');
        btnPrimary.classList.remove('btn-secondary');
        btnPrimary.classList.add('btn-success');
    }

    if (consoleLink && consoleMsg) {
        const dashboardLink = primaryEnv.dashboard_url || primaryEnv.dashboard_open_link || '';
        const canOpenDashboard = Boolean(primaryEnv.running && dashboardLink);
        if (canOpenDashboard) {
            consoleLink.classList.remove('disabled');
            consoleLink.href = dashboardLink;
            consoleMsg.style.display = 'none';
        } else {
            consoleLink.classList.add('disabled');
            consoleLink.href = '#';
            consoleMsg.style.display = 'block';
            consoleMsg.textContent = primaryEnv.running
                ? '运行环境当前不可打开控制台'
                : '运行环境未运行，无法访问控制台';
        }
    }
}

async function loadSnapshots(forceRefresh = false, offset = snapshotPageOffset) {
    try {
        const container = document.getElementById('snapshots-container');
        if (container && !offset) {
            container.innerHTML = '<div class="loading">快照加载中...</div>';
        }
        const response = await API.getSnapshots(forceRefresh, SNAPSHOT_PAGE_SIZE, offset);
        if (!response.success) return;
        snapshotPageOffset = Number(response.data.offset || 0);
        updateSnapshotsList(response.data);
    } catch (error) {
        console.error('加载快照失败:', error);
    }
}

function updateSnapshotsList(snapshotPayload) {
    const container = document.getElementById('snapshots-container');
    if (!container) return;
    const snapshots = snapshotPayload.snapshots || [];
    if (snapshots.length === 0) {
        container.innerHTML = '<div class="empty">暂无快照</div>';
        return;
    }
    const listHtml = snapshots.map(snapshot => `
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
    const prevDisabled = snapshotPageOffset <= 0 ? 'disabled' : '';
    const nextDisabled = snapshotPayload.has_more ? '' : 'disabled';
    container.innerHTML = `
        ${listHtml}
        <div class="snapshot-pagination">
            <button class="btn btn-small" ${prevDisabled} onclick="changeSnapshotPage(-1)">上一页</button>
            <span class="snapshot-page-status">第 ${Math.floor(snapshotPageOffset / SNAPSHOT_PAGE_SIZE) + 1} 页</span>
            <button class="btn btn-small" ${nextDisabled} onclick="changeSnapshotPage(1)">下一页</button>
        </div>
    `;
}

async function changeSnapshotPage(direction) {
    const nextOffset = Math.max(0, snapshotPageOffset + direction * SNAPSHOT_PAGE_SIZE);
    if (nextOffset === snapshotPageOffset && direction < 0) return;
    await loadSnapshots(false, nextOffset);
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
        await loadSnapshots(true, 0);
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

function showConfirmModal(action) {
    currentConfirmAction = { action };
    const modal = document.getElementById('confirm-modal');
    const messageEl = document.getElementById('confirm-message');
    const codeSection = document.querySelector('.confirm-input');
    if (!modal || !messageEl) return;

    if (action === 'restart') {
        messageEl.textContent = '此操作会重启当前 OpenClaw。重启期间消息通道插件与控制台会短暂不可用，健康守护助手会负责拉起并恢复。';
    }
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
    if (!currentConfirmAction) {
        showToast('操作信息丢失，请重试', 'error');
        return;
    }

    setConfirmActionLoading(true);
    try {
        if (currentConfirmAction.action === 'restart') {
            await executeRestart();
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

async function executeRestart() {
    const response = await API.restartEnvironment();
    if (!response.success) {
        throw new Error(response.error || response.data?.message || '重启失败');
    }
    showToast(response.data.message || 'Primary 已开始重启', 'success');
    setTimeout(() => window.location.reload(), 500);
}

async function executeEmergencyRecover() {
    if (!confirm('急救恢复会优先恢复最近配置快照并重启 OpenClaw。若快照不可用，会返回 known good 代码回退提示。确定执行吗？')) {
        return;
    }
    try {
        const response = await API.emergencyRecover();
        if (!response.success) {
            const guidance = response.data?.rollback_guidance?.target;
            throw new Error(`${response.data?.message || response.error || '急救恢复失败'}${guidance ? `；建议回退到 ${guidance}` : ''}`);
        }
        showToast(response.data.message || '急救恢复已执行', 'success');
        await loadManageData();
    } catch (error) {
        console.error('急救恢复失败:', error);
        showToast('急救恢复失败: ' + error.message, 'error');
    }
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
