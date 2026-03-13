/**
 * 总览视图JavaScript
 */

document.addEventListener('DOMContentLoaded', () => {
    initOverview();
});

async function initOverview() {
    // 初始加载所有数据
    await Promise.all([
        loadHealthScore(),
        loadMetrics(),
        loadEnvironment(),
        loadEvents()
    ]);
    
    // 设置自动刷新
    AutoRefresh.start('healthScore', loadHealthScore, AppState.refreshIntervals.healthScore);
    AutoRefresh.start('metrics', loadMetrics, AppState.refreshIntervals.metrics);
    AutoRefresh.start('events', loadEvents, AppState.refreshIntervals.events);
    AutoRefresh.start('environment', loadEnvironment, AppState.refreshIntervals.environment);
    
    // 绑定事件折叠按钮
    const toggleBtn = document.getElementById('toggle-events');
    const eventsContainer = document.getElementById('events-container');
    
    if (toggleBtn && eventsContainer) {
        toggleBtn.addEventListener('click', () => {
            eventsContainer.classList.toggle('collapsed');
            toggleBtn.textContent = eventsContainer.classList.contains('collapsed') ? '展开' : '折叠';
        });
    }
    
    // 绑定设置按钮
    const settingsBtn = document.getElementById('settings-btn');
    if (settingsBtn) {
        settingsBtn.addEventListener('click', showSettingsModal);
    }
    
    // 绑定设置弹窗按钮
    document.getElementById('settings-close')?.addEventListener('click', hideSettingsModal);
    document.getElementById('settings-cancel')?.addEventListener('click', hideSettingsModal);
    document.getElementById('settings-save')?.addEventListener('click', saveSettings);
    document.querySelector('#settings-modal .modal-overlay')?.addEventListener('click', hideSettingsModal);
    
    // 加载保存的设置
    loadSettings();
}

/**
 * 加载健康评分
 */
async function loadHealthScore() {
    try {
        const response = await API.getHealthScore();
        
        if (!response.success) {
            console.error('获取健康评分失败:', response.error);
            return;
        }
        
        const data = response.data;
        AppState.cache.healthScore = data;
        
        // 更新健康评分显示
        updateHealthScoreUI(data);
        
    } catch (error) {
        console.error('加载健康评分失败:', error);
    }
}

/**
 * 更新健康评分UI
 */
function updateHealthScoreUI(data) {
    // 更新分数
    const scoreValue = document.querySelector('.score-value');
    const scoreRing = document.getElementById('score-ring');
    const statusIcon = document.querySelector('.status-icon');
    const statusText = document.querySelector('.status-text');
    
    if (scoreValue) {
        scoreValue.textContent = data.score;
    }
    
    if (scoreRing) {
        scoreRing.className = 'score-ring ' + data.status;
    }
    
    if (statusIcon && statusText) {
        statusIcon.textContent = data.status_emoji;
        statusText.textContent = getStatusText(data.status);
        statusText.style.color = data.status_color;
    }
    
    // 更新下一步行动
    updateNextActionUI(data.next_action);
    
    // 更新最后更新时间
    UI.updateLastUpdate();
}

/**
 * 获取状态文本
 */
function getStatusText(status) {
    const statusMap = {
        'excellent': '系统健康',
        'good': '运行良好',
        'warning': '需要关注',
        'critical': '需要立即处理'
    };
    return statusMap[status] || '未知状态';
}

/**
 * 更新下一步行动UI
 */
function updateNextActionUI(action) {
    const actionCard = document.getElementById('next-action-card');
    const priorityEl = document.getElementById('action-priority');
    const messageEl = document.getElementById('action-message');
    const btnEl = document.getElementById('action-btn');
    
    if (!actionCard || !priorityEl || !messageEl || !btnEl) return;
    
    // 更新优先级标签
    priorityEl.textContent = action.priority_label;
    priorityEl.className = 'action-priority ' + action.priority;
    
    // 更新消息
    messageEl.textContent = action.message;
    
    // 更新按钮
    if (action.action) {
        btnEl.textContent = action.action;
        btnEl.style.display = 'inline-block';
        btnEl.style.background = action.color;
        
        btnEl.onclick = () => {
            if (action.action_type === 'view') {
                window.location.href = '/explore';
            }
        };
    } else {
        btnEl.style.display = 'none';
    }
    
    // 更新卡片样式
    actionCard.className = 'action-card ' + action.priority;
}

/**
 * 加载系统指标
 */
async function loadMetrics() {
    try {
        const response = await API.getMetrics();
        
        if (!response.success) {
            console.error('获取指标失败:', response.error);
            return;
        }
        
        const data = response.data;
        AppState.cache.metrics = data;
        
        // 更新指标UI
        updateMetricsUI(data);
        
    } catch (error) {
        console.error('加载指标失败:', error);
    }
}

/**
 * 更新指标UI
 */
function updateMetricsUI(data) {
    // CPU
    const cpuValue = document.getElementById('cpu-value');
    const cpuBar = document.getElementById('cpu-bar');
    
    if (cpuValue && cpuBar) {
        const cpu = data.cpu_percent || 0;
        cpuValue.textContent = UI.formatNumber(cpu, 0) + '%';
        cpuBar.style.width = Math.min(cpu, 100) + '%';
        cpuBar.className = 'progress-bar ' + getMetricClass(cpu, 70, 90);
    }
    
    // 内存
    const memValue = document.getElementById('mem-value');
    const memBar = document.getElementById('mem-bar');
    const memDetail = document.getElementById('mem-detail');
    
    if (memValue && memBar && memDetail) {
        const mem = data.memory_percent || 0;
        const used = data.memory_used_gb || 0;
        const total = data.memory_total_gb || 0;
        
        memValue.textContent = UI.formatNumber(mem, 0) + '%';
        memBar.style.width = Math.min(mem, 100) + '%';
        memBar.className = 'progress-bar ' + getMetricClass(mem, 70, 85);
        memDetail.textContent = `${UI.formatNumber(used, 1)} GB / ${UI.formatNumber(total, 1)} GB`;
    }
    
    // 会话（暂时显示模拟数据）
    const sessionsValue = document.getElementById('sessions-value');
    const sessionsBar = document.getElementById('sessions-bar');
    
    if (sessionsValue && sessionsBar) {
        const sessions = data.sessions || 0;
        sessionsValue.textContent = sessions;
        sessionsBar.style.width = Math.min((sessions / 20) * 100, 100) + '%';
    }
    
    // Gateway状态
    const gatewayValue = document.getElementById('gateway-value');
    const gatewayDetail = document.getElementById('gateway-detail');
    
    if (gatewayValue && gatewayDetail) {
        const healthy = Boolean(data.gateway_healthy);
        gatewayValue.textContent = healthy ? '运行中' : '异常';
        gatewayValue.style.color = healthy ? 'var(--color-success)' : 'var(--color-danger)';
        gatewayDetail.textContent = `PID: ${data.pid || '--'}`;
    }
}

/**
 * 获取指标样式类
 */
function getMetricClass(value, warningThreshold, dangerThreshold) {
    if (value >= dangerThreshold) return 'danger';
    if (value >= warningThreshold) return 'warning';
    return '';
}

/**
 * 加载环境信息
 */
async function loadEnvironment() {
    try {
        const response = await API.getEnvironment();
        
        if (!response.success) {
            console.error('获取环境失败:', response.error);
            return;
        }
        
        const data = response.data;
        AppState.cache.environment = data;
        
        // 更新环境UI
        updateEnvironmentUI(data);
        
    } catch (error) {
        console.error('加载环境失败:', error);
    }
}

/**
 * 更新环境UI
 */
function updateEnvironmentUI(data) {
    const envBadge = document.getElementById('active-env');
    const envId = document.getElementById('env-id');
    const envHealthy = document.getElementById('env-healthy');
    const codePath = document.getElementById('code-path');
    const statePath = document.getElementById('state-path');
    
    if (envBadge) {
        envBadge.textContent = (data.active_environment || 'unknown').toUpperCase();
    }
    
    if (envId) {
        envId.textContent = (data.active_environment || 'unknown').toUpperCase();
    }
    
    if (envHealthy) {
        const isHealthy = data.gateway_healthy;
        envHealthy.textContent = isHealthy ? '健康' : '异常';
        envHealthy.className = 'env-status ' + (isHealthy ? 'healthy' : 'unhealthy');
    }
    
    // 路径信息（如果有）
    if (codePath && data.code_path) {
        codePath.textContent = data.code_path;
    }
    
    if (statePath && data.state_path) {
        statePath.textContent = data.state_path;
    }
    
    // 更新OpenClaw控制台按钮状态
    updateConsoleButton(data);
}

/**
 * 更新OpenClaw控制台按钮状态
 */
function updateConsoleButton(data) {
    const consoleCard = document.getElementById('console-card');
    const consoleLink = document.getElementById('console-link');
    const consoleBadge = document.getElementById('console-status-badge');
    const consoleMessage = document.getElementById('console-message');
    
    if (!consoleLink || !consoleBadge) return;
    
    const activeEnv = data.active_environment || 'unknown';
    const gatewayHealthy = data.gateway_healthy || false;
    const isActive = activeEnv === 'primary' && gatewayHealthy;
    
    if (isActive) {
        // 激活态：可点击
        consoleLink.classList.remove('disabled');
        consoleLink.href = `http://127.0.0.1:8080?env=${activeEnv}`; // 根据实际端口调整
        consoleBadge.textContent = '已激活';
        consoleBadge.className = 'console-badge active';
        if (consoleCard) consoleCard.classList.remove('inactive');
        if (consoleMessage) consoleMessage.style.display = 'none';
    } else {
        // 非激活态：置灰禁用
        consoleLink.classList.add('disabled');
        consoleLink.href = '#';
        consoleBadge.textContent = '未激活';
        consoleBadge.className = 'console-badge inactive';
        if (consoleCard) consoleCard.classList.add('inactive');
        if (consoleMessage) {
            consoleMessage.textContent = activeEnv === 'official' 
                ? '当前激活的是Official环境，Primary环境未激活' 
                : '环境未激活或Gateway不健康';
            consoleMessage.style.display = 'block';
        }
    }
}

/**
 * 加载事件时间线
 */
async function loadEvents() {
    try {
        const response = await API.getEvents(20);
        
        if (!response.success) {
            console.error('获取事件失败:', response.error);
            return;
        }
        
        const data = response.data;
        AppState.cache.events = data.events;
        
        // 更新事件UI
        updateEventsUI(data.events);
        
    } catch (error) {
        console.error('加载事件失败:', error);
    }
}

/**
 * 更新事件UI
 */
function updateEventsUI(events) {
    const container = document.getElementById('events-timeline');
    
    if (!container) return;
    
    if (!events || events.length === 0) {
        container.innerHTML = '<div class="empty">暂无事件</div>';
        return;
    }
    
    // 渲染事件列表
    const html = events.map(event => {
        const type = event.type || 'info';
        const time = UI.formatTime(event.timestamp);
        const title = event.message || event.title || '未命名事件';
        const description = event.description || event.details?.question || '';
        
        return `
            <div class="event-item ${type}">
                <div class="event-time">${time}</div>
                <div class="event-content">
                    <div class="event-title">${title}</div>
                    ${description ? `<div class="event-description">${description}</div>` : ''}
                </div>
            </div>
        `;
    }).join('');
    
    container.innerHTML = html;
}

// ==================== 设置功能 ====================

let currentSettings = {
    refreshInterval: 5,
    notifyDesktop: true,
    showEvents: true
};

/**
 * 显示设置弹窗
 */
function showSettingsModal() {
    const modal = document.getElementById('settings-modal');
    if (!modal) return;
    
    // 加载当前设置到表单
    document.getElementById('settings-refresh').value = currentSettings.refreshInterval;
    document.getElementById('settings-notify-desktop').checked = currentSettings.notifyDesktop;
    document.getElementById('settings-show-events').checked = currentSettings.showEvents;
    
    modal.classList.add('active');
}

/**
 * 隐藏设置弹窗
 */
function hideSettingsModal() {
    const modal = document.getElementById('settings-modal');
    if (modal) {
        modal.classList.remove('active');
    }
}

/**
 * 保存设置
 */
function saveSettings() {
    // 获取表单值
    currentSettings.refreshInterval = parseInt(document.getElementById('settings-refresh').value);
    currentSettings.notifyDesktop = document.getElementById('settings-notify-desktop').checked;
    currentSettings.showEvents = document.getElementById('settings-show-events').checked;
    
    // 保存到本地存储
    localStorage.setItem('dashboardSettings', JSON.stringify(currentSettings));
    
    // 应用设置
    applySettings();
    
    // 关闭弹窗
    hideSettingsModal();
    
    // 显示提示
    showToast('设置已保存');
}

/**
 * 加载设置
 */
function loadSettings() {
    const saved = localStorage.getItem('dashboardSettings');
    if (saved) {
        try {
            currentSettings = JSON.parse(saved);
        } catch (e) {
            console.error('加载设置失败:', e);
        }
    }
    
    applySettings();
}

/**
 * 应用设置
 */
function applySettings() {
    // 应用刷新间隔
    AppState.refreshIntervals.healthScore = currentSettings.refreshInterval * 1000;
    AppState.refreshIntervals.metrics = currentSettings.refreshInterval * 1000;
    AppState.refreshIntervals.events = currentSettings.refreshInterval * 1000;
    
    // 重新启动自动刷新
    AutoRefresh.stopAll();
    AutoRefresh.start('healthScore', loadHealthScore, AppState.refreshIntervals.healthScore);
    AutoRefresh.start('metrics', loadMetrics, AppState.refreshIntervals.metrics);
    AutoRefresh.start('events', loadEvents, AppState.refreshIntervals.events);
    
    // 应用事件时间线显示
    const eventsSection = document.querySelector('.events-section');
    if (eventsSection) {
        eventsSection.style.display = currentSettings.showEvents ? 'block' : 'none';
    }
}

/**
 * 显示提示消息
 */
function showToast(message) {
    // 创建提示元素
    const toast = document.createElement('div');
    toast.className = 'toast';
    toast.textContent = message;
    toast.style.cssText = `
        position: fixed;
        bottom: 20px;
        right: 20px;
        background: var(--color-success);
        color: white;
        padding: 12px 24px;
        border-radius: 8px;
        font-size: 14px;
        z-index: 2000;
        animation: slideIn 0.3s ease;
    `;
    
    document.body.appendChild(toast);
    
    // 3秒后移除
    setTimeout(() => {
        toast.style.animation = 'slideOut 0.3s ease';
        setTimeout(() => toast.remove(), 300);
    }, 3000);
}

// 添加CSS动画
const style = document.createElement('style');
style.textContent = `
    @keyframes slideIn {
        from { transform: translateX(100%); opacity: 0; }
        to { transform: translateX(0); opacity: 1; }
    }
    @keyframes slideOut {
        from { transform: translateX(0); opacity: 1; }
        to { transform: translateX(100%); opacity: 0; }
    }
`;
document.head.appendChild(style);
