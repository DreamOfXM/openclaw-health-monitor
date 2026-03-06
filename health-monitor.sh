#!/bin/bash
# OpenClaw 健康监控系统 v10 - 兼容 macOS bash 3.x

OPENCLAW_DIR="$HOME/.openclaw"
WEBHOOK_FILE="$HOME/openclaw-health-monitor/config/webhook.txt"
LOCAL_WEBHOOK_FILE="$HOME/openclaw-health-monitor/config/webhook.local.txt"
LOG_FILE="$HOME/openclaw-health-monitor/logs/health-monitor.log"

mkdir -p "$(dirname "$LOG_FILE")"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$LOG_FILE"; }

send() {
    local content="$1"
    local webhook=""
    if [ -f "$LOCAL_WEBHOOK_FILE" ]; then
        webhook=$(cat "$LOCAL_WEBHOOK_FILE" 2>/dev/null)
    else
        webhook=$(cat "$WEBHOOK_FILE" 2>/dev/null)
    fi
    [ -z "$webhook" ] && return
    curl -s -X POST "$webhook" -H "Content-Type: application/json" \
        -d "{\"msgtype\":\"markdown\",\"markdown\":{\"title\":\"OpenClaw健康报告\",\"text\":$(echo "$content" | jq -Rs .)}}" > /dev/null
}

# UTC时间转Unix
utc2unix() {
    local ts="$1"
    local base=$(echo "$ts" | sed 's/\..*//')
    TZ=UTC date -j -f "%Y-%m-%dT%H:%M:%S" "$base" +%s 2>/dev/null || echo "0"
}

# 分析过去5分钟的对话（简化版，不用关联数组）
analyze() {
    local log="$OPENCLAW_DIR/logs/gateway.log"
    local now=$(date +%s)
    local ago=$((now - 300))
    local ago10=$((now - 600))  # 10分钟前的dispatch也算
    
    [ ! -f "$log" ] && echo "0 0 0 0" && return
    
    local total=0 slow=0 stuck=0 max=0
    local last_dispatch=""
    
    # 按时间顺序处理
    while IFS= read -r line; do
        local ts=$(echo "$line" | grep -oE '[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}')
        [ -z "$ts" ] && continue
        local unix=$(utc2unix "$ts")
        [ "$unix" = "0" ] && continue
        
        if echo "$line" | grep -q "dispatching to agent"; then
            # 记录最近的 dispatch（10分钟内的）
            if [ "$unix" -gt "$ago10" ]; then
                last_dispatch="$unix"
            fi
        elif echo "$line" | grep -q "dispatch complete"; then
            # 只统计 5 分钟内完成的对话
            if [ "$unix" -gt "$ago" ] && [ -n "$last_dispatch" ]; then
                local d=$((unix - last_dispatch))
                if [ "$d" -gt 0 ] && [ "$d" -lt 600 ]; then
                    total=$((total + 1))
                    [ "$d" -gt 30 ] && slow=$((slow + 1))
                    [ "$d" -gt 120 ] && stuck=$((stuck + 1))
                    [ "$d" -gt "$max" ] && max=$d
                fi
            fi
        fi
    done < <(tail -1000 "$log" 2>/dev/null)
    
    echo "$total $slow $stuck $max"
}

# CPU使用率
cpu_usage() {
    local line=$(top -l 1 2>/dev/null | grep "CPU usage")
    local user=$(echo "$line" | grep -oE '[0-9]+\.[0-9]+% user' | grep -oE '[0-9]+\.[0-9]+')
    local sys=$(echo "$line" | grep -oE '[0-9]+\.[0-9]+% sys' | grep -oE '[0-9]+\.[0-9]+')
    echo "$(echo "$user + $sys" | bc 2>/dev/null || echo "15")"
}

# 内存使用
mem_usage() {
    local line=$(top -l 1 2>/dev/null | grep "PhysMem")
    local used=$(echo "$line" | grep -oE '[0-9]+G used' | grep -oE '[0-9]+')
    local unused=$(echo "$line" | grep -oE '[0-9]+G unused' | grep -oE '[0-9]+')
    echo "${used:-16} ${unused:-20}"
}

# 服务状态
check_services() {
    local gw_pid=$(pgrep -f "openclaw.*gateway" 2>/dev/null | head -1)
    local gd_pid=$(pgrep -f "guardian" 2>/dev/null | head -1)
    local fs_pid=$(pgrep -f "fswatch" 2>/dev/null | head -1)
    
    [ -n "$gw_pid" ] && echo "Gateway ✅ (PID $gw_pid)" || echo "Gateway ❌ 未运行"
    [ -n "$gd_pid" ] && echo "Guardian v2 ✅ (PID $gd_pid)" || echo "Guardian v2 ❌ 未运行"
    [ -n "$fs_pid" ] && echo "fswatch ✅ (PID $fs_pid)" || echo "fswatch ❌ 未运行"
}

# 问题诊断
diagnose() {
    local issues=""
    local errs=$(tail -50 "$OPENCLAW_DIR/logs/gateway.err.log" 2>/dev/null | grep -ci "error" || echo "0")
    [ "$errs" -gt 10 ] && issues+="• 错误日志: ${errs}条\n"
    
    local comp=$(top -l 1 2>/dev/null | grep "PhysMem" | grep -oE '[0-9]+M compressor' | grep -oE '[0-9]+')
    [ -n "$comp" ] && [ "$comp" -gt 3000 ] && issues+="• 内存压缩: ${comp}MB\n"
    
    echo "$issues"
}

# 生成报告
report() {
    local now_str=$(date '+%Y-%m-%d %H:%M:%S')
    read total slow stuck max <<< $(analyze)
    local cpu=$(cpu_usage)
    read mem_used mem_unused <<< $(mem_usage)
    local svc=$(check_services)
    local issues=$(diagnose)
    
    local body="## 📊 OpenClaw 健康报告\n\n"
    body+="**时间**: $now_str\n\n"
    body+="---\n\n"
    body+="### 📈 对话分析（过去5分钟）\n\n"
    body+="| 指标 | 数值 |\n"
    body+="|:-----|:-----|\n"
    body+="| 总对话数 | $total |\n"
    body+="| 慢响应(>30s) | $slow |\n"
    body+="| 卡住(>2min) | $stuck |\n"
    body+="| 最长响应 | ${max}s |\n\n"
    body+="---\n\n"
    body+="### 💻 系统状态\n\n"
    body+="| 资源 | 使用 |\n"
    body+="|:-----|:-----|\n"
    body+="| CPU | ${cpu}% |\n"
    body+="| 内存 | ${mem_used}G / $((mem_used + mem_unused))G |\n\n"
    body+="### 服务状态\n\n"
    body+="$svc\n\n"
    
    if [ -n "$issues" ] || [ "$slow" -gt 0 ] || [ "$stuck" -gt 0 ]; then
        body+="---\n\n"
        body+="### ⚠️ 发现问题\n\n"
        [ "$slow" -gt 0 ] && body+="• 慢响应: $slow 次\n"
        [ "$stuck" -gt 0 ] && body+="• 卡住会话: $stuck 个\n"
        [ -n "$issues" ] && body+="$issues"
    else
        body+="---\n\n"
        body+="### ✅ 系统运行正常\n"
    fi
    
    echo "$body"
}

# 手动报告
manual() {
    local body=$(report)
    send "$body"
    echo "✅ 已发送健康报告"
    echo ""
    read total slow stuck max <<< $(analyze)
    echo "数据: 对话=$total, 慢响应=$slow, 卡住=$stuck, 最长=${max}s"
}

# 监控循环
main() {
    log "🚀 健康监控启动 v10"
    local last=0
    
    while true; do
        local now=$(date +%s)
        read total slow stuck max <<< $(analyze)
        local cpu=$(cpu_usage)
        read mem_used mem_unused <<< $(mem_usage)
        local issues=$(diagnose)
        
        log "📊 对话=$total 慢=$slow 卡=$stuck 最长=${max}s CPU=${cpu}% 内存=${mem_used}G"
        
        # 只在有问题时告警
        local alert=""
        [ "$stuck" -gt 0 ] && alert+="• 卡住会话: $stuck 个\n"
        [ "$slow" -gt 2 ] && alert+="• 慢响应: $slow 次\n"
        [ "$(echo "$cpu > 80" | bc 2>/dev/null)" = "1" ] && alert+="• CPU过高: ${cpu}%\n"
        [ "$mem_used" -gt 30 ] && alert+="• 内存过高: ${mem_used}G\n"
        [ -n "$issues" ] && alert+="$issues"
        pgrep -f "openclaw.*gateway" > /dev/null || alert+="• Gateway 未运行\n"
        
        if [ -n "$alert" ] && [ $((now - last)) -gt 600 ]; then
            local body=$(report)
            send "$body"
            last=$now
            log "📤 已推送告警"
        fi
        
        sleep 60
    done
}

case "${1:-}" in
    report) manual ;;
    *) main ;;
esac
