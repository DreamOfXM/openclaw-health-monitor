/**
 * 基础JavaScript
 * 全局状态和工具函数
 */

// 全局状态
const AppState = {
    currentView: 'overview',
    refreshInterval: null,
    lastUpdate: null,
    
    // 数据缓存
    cache: {
        healthScore: null,
        metrics: null,
        environment: null,
        events: null
    },
    
    // 刷新频率（毫秒）
    refreshIntervals: {
        healthScore: 5000,
        metrics: 5000,
        events: 5000,
        environment: 10000
    }
};

// API调用工具
const API = {
    /**
     * 发送GET请求
     */
    async get(url, params = {}) {
        const queryString = Object.keys(params)
            .map(key => `${encodeURIComponent(key)}=${encodeURIComponent(params[key])}`)
            .join('&');
        
        const fullUrl = queryString ? `${url}?${queryString}` : url;
        
        const response = await fetch(fullUrl);
        if (!response.ok) {
            throw new Error(`HTTP error! status: ${response.status}`);
        }
        return await response.json();
    },
    
    /**
     * 发送POST请求
     */
    async post(url, data = {}) {
        const response = await fetch(url, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(data)
        });
        
        if (!response.ok) {
            throw new Error(`HTTP error! status: ${response.status}`);
        }
        return await response.json();
    },
    
    /**
     * 获取健康评分
     */
    async getHealthScore(refresh = false) {
        return await this.get('/api/v2/health/score', { refresh });
    },
    
    /**
     * 获取系统指标
     */
    async getMetrics(refresh = false) {
        return await this.get('/api/v2/metrics', { refresh });
    },
    
    /**
     * 获取事件
     */
    async getEvents(limit = 20, refresh = false) {
        return await this.get('/api/v2/events', { limit, refresh });
    },
    
    /**
     * 获取环境信息
     */
    async getEnvironment(refresh = false) {
        return await this.get('/api/v2/environments', { refresh });
    },

    /**
     * 获取任务数据
     */
    async getTasks(refresh = false) {
        return await this.get('/api/v2/tasks', { refresh });
    },

    /**
     * 获取代理数据
     */
    async getAgents(refresh = false) {
        return await this.get('/api/v2/agents', { refresh });
    },

    /**
     * 获取学习数据
     */
    async getLearnings(refresh = false) {
        return await this.get('/api/v2/learnings', { refresh });
    },
    
    /**
     * 切换环境
     */
    async switchEnvironment(env) {
        return await this.post('/api/v2/environments/switch', { environment: env });
    },
    
    /**
     * 执行版本晋升
     */
    async promoteEnvironment(confirmation) {
        return await this.post('/api/v2/environments/promote', { confirmation });
    },

    async setOfficialAutoUpdate(enabled) {
        return await this.post('/api/v2/environments/official-auto-update', { enabled });
    },

    /**
     * 获取快照列表
     */
    async getSnapshots(refresh = false) {
        return await this.get('/api/v2/environments/snapshots', { refresh });
    },

    /**
     * 创建快照
     */
    async createSnapshot(label) {
        return await this.post('/api/v2/environments/snapshots', { label });
    },

    /**
     * 恢复快照
     */
    async restoreSnapshot(name) {
        return await this.post('/api/v2/environments/snapshots/restore', { name });
    }
};

// UI工具
const UI = {
    /**
     * 显示加载状态
     */
    showLoading(element) {
        element.innerHTML = '<div class="loading">加载中...</div>';
    },
    
    /**
     * 显示错误
     */
    showError(element, message) {
        element.innerHTML = `<div class="error">❌ ${message}</div>`;
    },
    
    /**
     * 格式化数字
     */
    formatNumber(num, decimals = 1) {
        return Number(num).toFixed(decimals);
    },
    
    /**
     * 格式化时间
     */
    formatTime(isoString) {
        if (!isoString) return '--';
        const date = new Date(isoString);
        return date.toLocaleTimeString('zh-CN', { 
            hour: '2-digit', 
            minute: '2-digit',
            second: '2-digit'
        });
    },
    
    /**
     * 格式化日期时间
     */
    formatDateTime(isoString) {
        if (!isoString) return '--';
        const date = new Date(isoString);
        return date.toLocaleString('zh-CN', {
            month: 'short',
            day: 'numeric',
            hour: '2-digit',
            minute: '2-digit'
        });
    },
    
    /**
     * 更新最后更新时间
     */
    updateLastUpdate() {
        const element = document.getElementById('last-update');
        if (element) {
            AppState.lastUpdate = new Date();
            element.textContent = `最后更新: ${this.formatTime(AppState.lastUpdate.toISOString())}`;
        }
    }
};

// 自动刷新管理
const AutoRefresh = {
    intervals: {},
    
    /**
     * 开始自动刷新
     */
    start(key, callback, interval) {
        if (this.intervals[key]) {
            clearInterval(this.intervals[key]);
        }
        
        this.intervals[key] = setInterval(callback, interval);
    },
    
    /**
     * 停止自动刷新
     */
    stop(key) {
        if (this.intervals[key]) {
            clearInterval(this.intervals[key]);
            delete this.intervals[key];
        }
    },
    
    /**
     * 停止所有自动刷新
     */
    stopAll() {
        Object.keys(this.intervals).forEach(key => {
            this.stop(key);
        });
    }
};

// 初始化
document.addEventListener('DOMContentLoaded', () => {
    // 初始化最后更新时间
    UI.updateLastUpdate();
    
    // 绑定刷新按钮
    const refreshBtn = document.getElementById('refresh-btn');
    if (refreshBtn) {
        refreshBtn.addEventListener('click', () => {
            window.location.reload();
        });
    }
    
    // 页面卸载时清理
    window.addEventListener('beforeunload', () => {
        AutoRefresh.stopAll();
    });
});

// 导出全局变量
window.AppState = AppState;
window.API = API;
window.UI = UI;
window.AutoRefresh = AutoRefresh;
