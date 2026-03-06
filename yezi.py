#!/usr/bin/env python3
"""
叶子 - OpenClaw 语音播报助手
纯播报功能，无语音识别
"""

import os
import sys
import time
import threading
import pyttsx3
from datetime import datetime

ASSISTANT_NAME = "叶子"
LOG_FILE = os.path.expanduser("~/openclaw-health-monitor/logs/yezi.log")

class YeZiAssistant:
    def __init__(self):
        self.tts_engine = pyttsx3.init()
        self.is_speaking = False
        self.running = True
        self.message_queue = []
        self.queue_lock = threading.Lock()
        
        self._setup_tts()
        self.log(f"🌸 {ASSISTANT_NAME} 初始化完成")
    
    def log(self, msg):
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        message = f"[{timestamp}] {msg}"
        print(message)
        try:
            os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
            with open(LOG_FILE, 'a') as f:
                f.write(message + '\n')
        except:
            pass
    
    def _setup_tts(self):
        voices = self.tts_engine.getProperty('voices')
        self.log(f"🎤 检测到 {len(voices)} 个声音")
        for i, voice in enumerate(voices):
            self.log(f"  {i}: {voice.name}")
        
        if len(voices) > 0:
            self.tts_engine.setProperty('voice', voices[0].id)
        self.tts_engine.setProperty('rate', 180)
    
    def speak(self, text):
        self.is_speaking = True
        self.log(f"🔊 {ASSISTANT_NAME}: {text}")
        
        try:
            self.tts_engine.say(text)
            self.tts_engine.runAndWait()
        except Exception as e:
            self.log(f"❌ 语音输出错误: {e}")
        
        self.is_speaking = False
    
    def stop(self):
        self.tts_engine.stop()
        self.is_speaking = False
    
    def queue_speak(self, text):
        with self.queue_lock:
            self.message_queue.append(text)
    
    def process_queue(self):
        while self.running:
            with self.queue_lock:
                if self.message_queue:
                    text = self.message_queue.pop(0)
                    self.speak(text)
                else:
                    time.sleep(0.5)
    
    def switch_voice(self):
        voices = self.tts_engine.getProperty('voices')
        current = self.tts_engine.getProperty('voice')
        
        for voice in voices:
            if voice.id != current:
                self.tts_engine.setProperty('voice', voice.id)
                self.speak(f"已切换到：{voice.name}")
                return
        
        self.speak("已切换声音")
    
    def run(self):
        print(f"""
╔═══════════════════════════════════════════════════════════╗
║                                                           ║
║     🌸 叶子 - 语音播报助手                                ║
║                                                           ║
║     功能：                                                ║
║     • 调用 speak() 方法进行语音播报                       ║
║     • 支持多线程调用 queue_speak() 排队播报              ║
║     • 可用 switch_voice() 切换声音                        ║
║                                                           ║
║     按 Ctrl+C 退出                                        ║
║                                                           ║
╚═══════════════════════════════════════════════════════════╝
""")
        
        self.speak(f"你好，我是{ASSISTANT_NAME}，语音播报助手已就绪")
        
        try:
            while self.running:
                time.sleep(1)
        except KeyboardInterrupt:
            self.log("🛑 用户退出")
            self.running = False
            self.speak("再见")

def main():
    assistant = YeZiAssistant()
    assistant.run()

if __name__ == "__main__":
    main()