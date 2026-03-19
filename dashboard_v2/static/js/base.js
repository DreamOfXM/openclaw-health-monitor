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
        
        const response = await fetch(fullUrl, {
            cache: 'no-store'
        });
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
     * 重启当前运行时
     */
    async restartEnvironment() {
        return await this.post('/api/v2/environments/restart', {});
    },

    async emergencyRecover() {
        return await this.post('/api/v2/environments/emergency-recover', {});
    },

    /**
     * 获取快照列表
     */
    async getSnapshots(refresh = false, limit = 20, offset = 0) {
        return await this.get('/api/v2/environments/snapshots', { refresh, limit, offset });
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

// SSE实时推送管理
const RealtimePush = {
    connections: {},
    reconnectDelay: 3000,
    maxReconnectDelay: 30000,
    
    /**
     * 连接SSE流
     */
    connect(endpoint, onMessage, onError) {
        if (this.connections[endpoint]) {
            this.disconnect(endpoint);
        }
        
        const eventSource = new EventSource(endpoint);
        const self = this;
        
        eventSource.onmessage = function(event) {
            try {
                const data = JSON.parse(event.data);
                if (data.type === 'heartbeat') {
                    return;
                }
                if (onMessage) {
                    onMessage(data);
                }
            } catch (e) {
                console.error('SSE parse error:', e);
            }
        };
        
        eventSource.onerror = function(error) {
            console.error('SSE connection error:', error);
            eventSource.close();
            delete self.connections[endpoint];
            
            if (onError) {
                onError(error);
            }
            
            setTimeout(() => {
                console.log('Reconnecting SSE:', endpoint);
                self.connect(endpoint, onMessage, onError);
            }, self.reconnectDelay);
        };
        
        eventSource.onopen = function() {
            console.log('SSE connected:', endpoint);
        };
        
        this.connections[endpoint] = eventSource;
        return eventSource;
    },
    
    /**
     * 断开SSE连接
     */
    disconnect(endpoint) {
        if (this.connections[endpoint]) {
            this.connections[endpoint].close();
            delete this.connections[endpoint];
        }
    },
    
    /**
     * 断开所有连接
     */
    disconnectAll() {
        Object.keys(this.connections).forEach(endpoint => {
            this.disconnect(endpoint);
        });
    },
    
    /**
     * 启动健康状态实时推送
     */
    startHealthStream(onUpdate) {
        return this.connect('/api/stream/health', (data) => {
            if (data.type === 'health' && onUpdate) {
                onUpdate(data.data);
                AppState.cache.healthScore = data.data;
                UI.updateLastUpdate();
            }
        });
    },
    
    /**
     * 启动系统指标实时推送
     */
    startMetricsStream(onUpdate) {
        return this.connect('/api/stream/metrics', (data) => {
            if (data.type === 'metrics' && onUpdate) {
                onUpdate(data.data);
                AppState.cache.metrics = data.data;
                UI.updateLastUpdate();
            }
        });
    },
    
    /**
     * 启动事件实时推送
     */
    startEventsStream(onUpdate) {
        return this.connect('/api/stream/events', (data) => {
            if ((data.type === 'events' || data.type === 'event') && onUpdate) {
                onUpdate(data.data, data.count);
                AppState.cache.events = data.data;
                UI.updateLastUpdate();
            }
        });
    }
};

// 初始化
document.addEventListener('DOMContentLoaded', () => {
    UI.updateLastUpdate();
    
    const refreshBtn = document.getElementById('refresh-btn');
    if (refreshBtn) {
        refreshBtn.addEventListener('click', () => {
            window.location.reload();
        });
    }
    
    window.addEventListener('beforeunload', () => {
        AutoRefresh.stopAll();
        RealtimePush.disconnectAll();
    });
});

// 导出全局变量
window.AppState = AppState;
window.API = API;
window.UI = UI;
window.AutoRefresh = AutoRefresh;
window.RealtimePush = RealtimePush;
