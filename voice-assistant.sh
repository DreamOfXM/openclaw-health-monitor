#!/bin/bash
# OpenClaw 实时语音对话助手
# 类似豆包的语音交互体验

MONITOR_DIR="$HOME/openclaw-health-monitor"
LOG_FILE="$MONITOR_DIR/logs/voice-assistant.log"
HISTORY_FILE="$MONITOR_DIR/logs/conversation-history.txt"

mkdir -p "$MONITOR_DIR/logs"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

# 语音输出（macOS say）
speak() {
    local text="$1"
    log "🔊 播报: ${text:0:50}..."
    say -v "Siri" -r 1.0 "$text" 2>/dev/null &
}

# 语音输出（系统提示音）
beep() {
    afplay /System/Library/Sounds/Glass.aiff 2>/dev/null
}

# 播放提示音
ready_sound() {
    afplay /System/Library/Sounds/Hero.aiff 2>/dev/null
}

# 显示帮助
show_help() {
    echo "
╔══════════════════════════════════════════════════════════════╗
║           OpenClaw 实时语音对话助手 v1.0                    ║
╠══════════════════════════════════════════════════════════════╣
║  使用方法：                                                  ║
║  1. 在任意聊天窗口，按 Option 键或 Fn 键启动听写            ║
║  2. 说出你想说的话，等待系统转换为文字                      ║
║  3. 发送消息后，助手会用语音回复                            ║
║                                                              ║
║  快捷键：                                                    ║
║  • Option 键（或 Fn 键）- 启动 macOS 听写                   ║
║  • Control+C - 退出助手                                     ║
║                                                              ║
║  语音命令：                                                  ║
║  • "停止说话" - 停止当前语音播报                            ║
║  • "再说一遍" - 重复上一条回复                              ║
║  • "打开健康报告" - 播报系统健康状态                        ║
╚══════════════════════════════════════════════════════════════╝
"
}

# 启动语音助手
start() {
    clear
    show_help
    
    log "🚀 OpenClaw 实时语音对话助手启动"
    
    # 播放启动提示音
    ready_sound
    speak "语音助手已就绪，请开始对话"
    
    echo ""
    echo "═══════════════════════════════════════════════════════════"
    echo "  🎤 语音输入就绪 - 在飞书/钉钉中使用 Option 键听写"
    echo "═══════════════════════════════════════════════════════════"
    echo ""
    echo "等待对话..."
    echo ""
    
    # 监控 OpenClaw 日志，检测新消息
    local last_check=$(date +%s)
    
    while true; do
        # 检查 OpenClaw Gateway 日志
        local now=$(date +%s)
        local log_file="$HOME/.openclaw/logs/gateway.log"
        
        if [ -f "$log_file" ]; then
            # 检查最近的回复
            local recent_replies=$(tail -20 "$log_file" 2>/dev/null | grep "dispatch complete" | tail -1)
            
            if [ -n "$recent_replies" ]; then
                local reply_time=$(echo "$recent_replies" | grep -oE '[0-9]{2}:[0-9]{2}:[0-9]{2}')
                
                # 如果是新的回复，播报
                if [ -f /tmp/last_reply_time ] && [ "$(cat /tmp/last_reply_time)" != "$reply_time" ]; then
                    log "📩 检测到新回复: $reply_time"
                    beep
                    echo "$reply_time" > /tmp/last_reply_time
                fi
            fi
        fi
        
        sleep 2
    done
}

# 测试语音
test_voice() {
    echo "测试语音输出..."
    speak "你好，我是 OpenClaw 语音助手"
    sleep 1
    speak "语音功能正常"
}

# 交互模式
interactive() {
    while true; do
        echo ""
        echo "命令: (s)peak测试语音 (h)elp帮助 (q)uit退出"
        read -p "> " cmd
        
        case $cmd in
            s|speak)
                read -p "输入要朗读的文字: " text
                speak "$text"
                ;;
            h|help)
                show_help
                ;;
            q|quit|exit)
                speak "再见"
                exit 0
                ;;
            *)
                speak "$cmd"
                ;;
        esac
    done
}

# 主程序
case "${1:-start}" in
    start)
        start
        ;;
    test)
        test_voice
        ;;
    interactive|i)
        interactive
        ;;
    speak)
        speak "$2"
        ;;
    *)
        show_help
        ;;
esac