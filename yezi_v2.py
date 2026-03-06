#!/usr/bin/env python3
"""叶子语音助手 v2 - 使用 Ollama 本地模型"""

import os
import sys
import speech_recognition as sr
import pyttsx3
import requests
import threading

# 配置
OLLAMA_URL = "http://localhost:11434"
MODEL_NAME = "qwen3:30b"  # 先用 30B 测试

print("""
╔═══════════════════════════════════════════════════════════╗
║     🌸 叶子 - 实时语音助手 (Ollama 本地版)                ║
║                                                           ║
║     模型: {}                                              ║
║                                                           ║
║     使用方法：                                            ║
║     • 直接说话即可，无需按键                              ║
║     • 说"叶子"唤醒我                                      ║
║     • 说"退出"结束                                        ║
║                                                           ║
╚═══════════════════════════════════════════════════════════╝
""".format(MODEL_NAME))

# 初始化语音合成
engine = pyttsx3.init()
voices = engine.getProperty('voices')
if len(voices) > 1:
    engine.setProperty('voice', voices[1].id)  # 用第二个声音
engine.setProperty('rate', 180)

# 初始化语音识别
recognizer = sr.Recognizer()
microphone = sr.Microphone()

def speak(text):
    """语音输出"""
    print(f"🔊 叶子: {text}")
    engine.say(text)
    engine.runAndWait()

def think(prompt):
    """调用 Ollama 本地模型"""
    try:
        response = requests.post(
            f"{OLLAMA_URL}/api/generate",
            json={
                "model": MODEL_NAME,
                "prompt": prompt,
                "stream": False,
                "options": {
                    "temperature": 0.7,
                    "num_predict": 200
                }
            },
            timeout=120  # 30B 模型需要更长时间
        )
        if response.status_code == 200:
            return response.json().get("response", "")
        else:
            return f"模型错误: {response.status_code}"
    except Exception as e:
        return f"连接错误: {str(e)}"

def listen():
    """语音输入"""
    with microphone as source:
        recognizer.adjust_for_ambient_noise(source, duration=1)
        print("🎤 叶子 开始监听...")
        audio = recognizer.listen(source, phrase_time_limit=10)
    
    print("🔄 识别中...")
    try:
        # 尝试用 Sphinx 离线识别（英文为主）
        text = recognizer.recognize_sphinx(audio)
        return text
    except:
        pass
    
    try:
        # 尝试 Google（需要网络）
        text = recognizer.recognize_google(audio, language="zh-CN")
        return text
    except Exception as e:
        print(f"❌ 识别失败: {e}")
        return None

# 启动
speak("你好，我是叶子，现在开始对话")

while True:
    try:
        print("\n👂 等待说话...")
        text = listen()
        
        if not text:
            continue
            
        print(f"你说: {text}")
        
        # 检查命令
        if "退出" in text or "再见" in text:
            speak("再见，下次再聊")
            break
        
        if "你好" in text.lower():
            speak("你好呀，有什么可以帮你的")
            continue
        
        # 调用模型（后台执行，避免卡住）
        print("🤔 思考中...")
        import time
        start = time.time()
        
        # 用线程执行
        result = []
        def worker():
            r = think(text)
            result.append(r)
        
        t = threading.Thread(target=worker)
        t.start()
        t.join(timeout=60)  # 最多等60秒
        
        if t.is_alive():
            print("⏳ 模型响应超时，用默认回复")
            speak("这个问题有点复杂，我需要更多时间思考")
        elif result:
            response = result[0][:200]  # 限制长度
            print(f"💭 回复: {response}")
            speak(response)
            
        print(f"⏱️ 耗时: {time.time()-start:.1f}秒")
        
    except KeyboardInterrupt:
        speak("再见")
        break
    except Exception as e:
        print(f"错误: {e}")

print("\n已退出叶子语音助手")