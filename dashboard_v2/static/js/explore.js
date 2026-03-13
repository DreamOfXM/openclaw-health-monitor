/**
 * 详情视图JavaScript
 */

document.addEventListener('DOMContentLoaded', () => {
    initExplore();
});

let currentTab = 'environments';
let currentLearningView = 'items';
let currentLearningData = null;

function initExplore() {
    // 绑定标签切换
    const navButtons = document.querySelectorAll('.nav-btn');
    navButtons.forEach(btn => {
        btn.addEventListener('click', () => {
            const tabName = btn.dataset.tab;
            switchTab(tabName);
        });
    });
    
    // 初始加载
    loadEnvironments();
    
    // 绑定刷新按钮
    document.getElementById('refresh-env')?.addEventListener('click', () => loadEnvironments(true));
    document.getElementById('refresh-tasks')?.addEventListener('click', () => loadTasks(true));
    document.getElementById('refresh-agents')?.addEventListener('click', () => loadAgents(true));
    document.getElementById('refresh-learnings')?.addEventListener('click', () => loadLearnings(true));
    
    // 任务过滤器
    const filterButtons = document.querySelectorAll('.filter-btn');
    filterButtons.forEach(btn => {
        btn.addEventListener('click', () => {
            const filter = btn.dataset.filter;
            filterTasks(filter);
            
            // 更新active状态
            filterButtons.forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
        });
    });
    
    // 绑定复制按钮
    bindCopyButtons();

    const learningCards = document.querySelectorAll('.stat-card-clickable');
    learningCards.forEach(card => {
        card.addEventListener('click', () => {
            currentLearningView = card.dataset.learningView || 'items';
            updateLearningViewSelection();
            if (currentLearningData) {
                renderLearningDetails(currentLearningData);
            }
        });
    });
}

function switchTab(tabName) {
    // 更新导航按钮
    document.querySelectorAll('.nav-btn').forEach(btn => {
        btn.classList.remove('active');
        if (btn.dataset.tab === tabName) {
            btn.classList.add('active');
        }
    });
    
    // 更新内容区
    document.querySelectorAll('.tab-content').forEach(content => {
        content.classList.remove('active');
    });
    document.getElementById(`tab-${tabName}`)?.classList.add('active');
    
    // 加载对应数据
    currentTab = tabName;
    switch(tabName) {
        case 'environments':
            loadEnvironments();
            break;
        case 'tasks':
            loadTasks();
            break;
        case 'agents':
            loadAgents();
            break;
        case 'learnings':
            loadLearnings();
            break;
    }
}

/**
 * 加载环境详情
 */
async function loadEnvironments(forceRefresh = false) {
    try {
        const response = await API.getEnvironment(forceRefresh);
        if (!response.success) return;
        
        const data = response.data;
        updateEnvironmentDetails(data);
        
    } catch (error) {
        console.error('加载环境失败:', error);
    }
}

function updateEnvironmentDetails(data) {
    const primaryEnv = data.environments?.find(e => e.id === 'primary') || {};
    const officialEnv = data.environments?.find(e => e.id === 'official') || {};
    updateSingleEnvironment('primary', primaryEnv);
    updateSingleEnvironment('official', officialEnv);
    updateEnvironmentComparison(primaryEnv, officialEnv);
}

function updateSingleEnvironment(prefix, env) {
    document.getElementById(`${prefix}-code-path`).textContent = env.code_path || '--';
    document.getElementById(`${prefix}-state-path`).textContent = env.state_path || '--';
    document.getElementById(`${prefix}-git`).textContent = (env.git_head || '--').substring(0, 8);
    document.getElementById(`${prefix}-pid`).textContent = env.pid || '--';

    const token = env.token || '--';
    document.getElementById(`${prefix}-token`).textContent = token === '--'
        ? token
        : `${token.substring(0, 10)}...`;

    const badge = document.getElementById(`${prefix}-status-badge`);
    if (!badge) return;

    const status = getEnvironmentStatus(env);
    badge.textContent = status.label;
    badge.className = `env-status-badge ${status.className}`;
}

function getEnvironmentStatus(env) {
    if (!env || Object.keys(env).length === 0) {
        return { label: '未知', className: 'inactive' };
    }
    if (env.active && env.running && env.healthy) {
        return { label: '激活 / 健康', className: 'active' };
    }
    if (env.running && env.healthy) {
        return { label: '运行中', className: 'healthy' };
    }
    if (env.running && !env.healthy) {
        return { label: '运行异常', className: 'warning' };
    }
    if (env.active && !env.running) {
        return { label: '激活未运行', className: 'warning' };
    }
    return { label: '待机', className: 'inactive' };
}

function updateEnvironmentComparison(primaryEnv, officialEnv) {
    document.getElementById('compare-primary-version').textContent = shortHead(primaryEnv.git_head);
    document.getElementById('compare-official-version').textContent = shortHead(officialEnv.git_head);
    document.getElementById('compare-version-diff').textContent =
        primaryEnv.git_head && officialEnv.git_head && primaryEnv.git_head !== officialEnv.git_head
            ? '不同'
            : '一致';

    document.getElementById('compare-primary-uptime').textContent = runtimeLabel(primaryEnv);
    document.getElementById('compare-official-uptime').textContent = runtimeLabel(officialEnv);
    document.getElementById('compare-uptime-diff').textContent =
        primaryEnv.running !== officialEnv.running ? '运行状态不同' : '同态';

    document.getElementById('compare-primary-health').textContent = healthLabel(primaryEnv);
    document.getElementById('compare-official-health').textContent = healthLabel(officialEnv);
    document.getElementById('compare-health-diff').textContent =
        healthLabel(primaryEnv) === healthLabel(officialEnv) ? '一致' : '不同';
}

function shortHead(value) {
    return (value || '--').substring(0, 8);
}

function runtimeLabel(env) {
    if (!env || Object.keys(env).length === 0) return '--';
    if (env.active && env.running) return '激活运行中';
    if (env.running) return '运行中';
    return '未运行';
}

function healthLabel(env) {
    if (!env || Object.keys(env).length === 0) return '--';
    if (env.running && env.healthy) return '健康';
    if (env.running) return '异常';
    return '待机';
}

/**
 * 加载任务列表
 */
async function loadTasks(forceRefresh = false) {
    try {
        const container = document.getElementById('tasks-list');
        if (forceRefresh && container) {
            container.innerHTML = '<div class="loading">刷新任务数据中...</div>';
        }
        const response = await API.getTasks(forceRefresh);
        if (!response.success) return;
        
        const data = response.data;
        updateTasksList(data.tasks || []);
        
    } catch (error) {
        console.error('加载任务失败:', error);
    }
}

function updateTasksList(tasks) {
    const container = document.getElementById('tasks-list');
    if (!container) return;
    
    if (tasks.length === 0) {
        container.innerHTML = '<div class="empty">暂无任务</div>';
        return;
    }
    
    const html = tasks.map(task => `
        <div class="task-item ${task.status}">
            <div class="task-header">
                <span class="task-id">#${task.id}</span>
                <span class="task-status badge badge-${task.status}">${taskStatusLabel(task)}</span>
            </div>
            <div class="task-body">
                <p class="task-name">${task.name || '未命名任务'}</p>
                <p class="task-meta">
                    <span>创建时间: ${UI.formatDateTime(task.created)}</span>
                    <span>代理: ${task.agent || '--'}</span>
                    <span>阶段: ${task.current_stage || '--'}</span>
                </p>
            </div>
        </div>
    `).join('');
    
    container.innerHTML = html;
}

function taskStatusLabel(task) {
    if (task.raw_status === 'background') return '处理中';
    if (task.status === 'running') return '运行中';
    if (task.status === 'blocked') return '阻塞';
    if (task.status === 'completed') return '已完成';
    return task.status || '未知';
}

function filterTasks(filter) {
    const container = document.getElementById('tasks-list');
    const items = container?.querySelectorAll('.task-item');
    
    if (!items) return;
    
    items.forEach(item => {
        if (filter === 'all' || item.classList.contains(filter)) {
            item.style.display = 'block';
        } else {
            item.style.display = 'none';
        }
    });
}

/**
 * 加载代理列表
 */
async function loadAgents(forceRefresh = false) {
    try {
        const container = document.getElementById('agents-grid');
        if (forceRefresh && container) {
            container.innerHTML = '<div class="loading">刷新代理数据中...</div>';
        }
        const response = await API.getAgents(forceRefresh);
        if (!response.success) return;
        
        const data = response.data;
        updateAgentsGrid(data.agents || []);
        
    } catch (error) {
        console.error('加载代理失败:', error);
    }
}

function updateAgentsGrid(agents) {
    const container = document.getElementById('agents-grid');
    if (!container) return;
    
    if (agents.length === 0) {
        container.innerHTML = '<div class="empty">暂无活跃代理</div>';
        return;
    }
    
    const html = agents.map(agent => `
        <div class="agent-card">
            <div class="agent-header">
                <span class="agent-name">${agent.name || agent.id}</span>
                <span class="agent-status badge badge-${agent.is_active ? 'success' : 'secondary'}">
                    ${agent.is_active ? '活跃' : '空闲'}
                </span>
            </div>
            <div class="agent-body">
                <p>最后活动: ${UI.formatDateTime(agent.last_activity)}</p>
                <p>会话数: ${agent.sessions || 0}</p>
            </div>
        </div>
    `).join('');
    
    container.innerHTML = html;
}

/**
 * 加载学习数据
 */
async function loadLearnings(forceRefresh = false) {
    try {
        const container = document.getElementById('learnings-list');
        if (forceRefresh && container) {
            container.innerHTML = '<div class="loading">刷新学习数据中...</div>';
        }
        const response = await API.getLearnings(forceRefresh);
        if (!response.success) return;
        
        const data = response.data;
        updateLearnings(data);
        
    } catch (error) {
        console.error('加载学习数据失败:', error);
    }
}

function updateLearnings(data) {
    currentLearningData = data;

    // 更新统计
    document.getElementById('learning-count').textContent = data.items?.length || 0;
    document.getElementById('reflection-count').textContent = data.reflections?.length || 0;
    document.getElementById('promoted-count').textContent = data.promoted?.length || 0;
    updateLearningViewSelection();
    renderLearningDetails(data);
}

function updateLearningViewSelection() {
    document.querySelectorAll('.stat-card-clickable').forEach(card => {
        card.classList.toggle('active', card.dataset.learningView === currentLearningView);
    });
}

function renderLearningDetails(data) {
    const container = document.getElementById('learnings-list');
    const titleEl = document.getElementById('learning-detail-title');
    if (!container || !titleEl) return;

    const viewConfig = {
        items: { title: '学习项明细', items: data.items || [] },
        reflections: { title: '反思记录明细', items: data.reflections || [] },
        promoted: { title: '已晋升明细', items: data.promoted || [] }
    };
    const selected = viewConfig[currentLearningView] || viewConfig.items;
    titleEl.textContent = selected.title;

    if (selected.items.length === 0) {
        container.innerHTML = '<div class="empty">暂无明细</div>';
        return;
    }

    if (currentLearningView === 'reflections') {
        container.innerHTML = selected.items.map(renderReflectionItem).join('');
        return;
    }

    container.innerHTML = selected.items.map(renderLearningItem).join('');
}

function renderLearningItem(item) {
    const meta = [];
    if (item.status) meta.push(`<span class="learning-pill">状态: ${item.status}</span>`);
    if (item.category) meta.push(`<span class="learning-pill">分类: ${item.category}</span>`);
    if (item.occurrences) meta.push(`<span class="learning-pill">出现: ${item.occurrences}</span>`);
    if (item.promoted_target) meta.push(`<span class="learning-pill">晋升目标: ${item.promoted_target}</span>`);

    return `
        <div class="learning-item">
            <div class="learning-header">
                <span class="learning-title">${item.title || '未命名'}</span>
                <span class="learning-date">${UI.formatDateTime(item.timestamp)}</span>
            </div>
            <p class="learning-desc">${item.description || ''}</p>
            <div class="learning-meta">${meta.join('')}</div>
        </div>
    `;
}

function renderReflectionItem(item) {
    const summary = item.summary || {};
    const meta = [];
    if (item.run_type) meta.push(`<span class="learning-pill">类型: ${item.run_type}</span>`);
    Object.entries(summary).slice(0, 4).forEach(([key, value]) => {
        meta.push(`<span class="learning-pill">${key}: ${value}</span>`);
    });

    return `
        <div class="learning-item">
            <div class="learning-header">
                <span class="learning-title">反思运行</span>
                <span class="learning-date">${UI.formatDateTime(item.created_at)}</span>
            </div>
            <p class="learning-desc">${Object.keys(summary).length ? JSON.stringify(summary) : '无额外摘要'}</p>
            <div class="learning-meta">${meta.join('')}</div>
        </div>
    `;
}

// ==================== 复制功能 ====================

/**
 * 绑定复制按钮
 */
function bindCopyButtons() {
    const copyButtons = document.querySelectorAll('.btn-copy');
    copyButtons.forEach(btn => {
        btn.addEventListener('click', (e) => {
            e.stopPropagation();
            const targetId = btn.dataset.target;
            const targetElement = document.getElementById(targetId);
            
            if (targetElement) {
                const text = targetElement.textContent.trim();
                if (text && text !== '--') {
                    copyToClipboard(text, btn);
                }
            }
        });
    });
}

/**
 * 复制到剪贴板
 */
async function copyToClipboard(text, btn) {
    try {
        await navigator.clipboard.writeText(text);
        
        // 显示复制成功状态
        const originalHTML = btn.innerHTML;
        btn.classList.add('copied');
        btn.innerHTML = `
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <polyline points="20 6 9 17 4 12"></polyline>
            </svg>
        `;
        btn.title = '已复制!';
        
        // 2秒后恢复原状
        setTimeout(() => {
            btn.classList.remove('copied');
            btn.innerHTML = originalHTML;
            btn.title = '复制';
        }, 2000);
        
        // 显示提示
        showCopyToast('已复制到剪贴板');
        
    } catch (err) {
        console.error('复制失败:', err);
        showCopyToast('复制失败，请手动复制', 'error');
    }
}

/**
 * 显示复制提示
 */
function showCopyToast(message, type = 'success') {
    // 创建提示元素
    const toast = document.createElement('div');
    toast.className = 'copy-toast';
    toast.textContent = message;
    toast.style.cssText = `
        position: fixed;
        top: 20px;
        right: 20px;
        background: ${type === 'success' ? 'var(--color-success)' : 'var(--color-danger)'};
        color: white;
        padding: 12px 20px;
        border-radius: 8px;
        font-size: 14px;
        z-index: 2000;
        animation: slideInRight 0.3s ease;
        box-shadow: 0 4px 12px rgba(0, 0, 0, 0.3);
    `;
    
    document.body.appendChild(toast);
    
    // 2秒后移除
    setTimeout(() => {
        toast.style.animation = 'slideOutRight 0.3s ease';
        setTimeout(() => toast.remove(), 300);
    }, 2000);
}

// 添加CSS动画
const copyStyle = document.createElement('style');
copyStyle.textContent = `
    @keyframes slideInRight {
        from { transform: translateX(100%); opacity: 0; }
        to { transform: translateX(0); opacity: 1; }
    }
    @keyframes slideOutRight {
        from { transform: translateX(0); opacity: 1; }
        to { transform: translateX(100%); opacity: 0; }
    }
`;
document.head.appendChild(copyStyle);
