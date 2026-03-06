#!/usr/bin/env python3
"""叶子语音助手 - 键盘输入测试版"""

import requests
import pyttsx3
import time
import threading

OLLAMA_URL = "http://localhost:11434"
MODEL_NAME = "qwen2.5:7b"

print("""
╔═══════════════════════════════════════════════════════════╗
║     🌸 叶子 - 测试版（键盘输入）                          ║
║                                                           ║
║     模型: {}                                              ║
║     提示: 输入文字，叶子会用语音回复                      ║
║     退出: 输入"退出"                                       ║
╚═══════════════════════════════════════════════════════════╝
""".format(MODEL_NAME))

engine = pyttsx3.init()
engine.setProperty('rate', 180)

def speak(text):
    print(f"🔊 叶子: {text}")
    try:
        engine.say(text)
        engine.runAndWait()
    except Exception as e:
        print(f"语音错误: {e}")
        engine.stop()

def ask(text):
    """调用 Ollama - 非流式"""
    try:
        r = requests.post(
            f"{OLLAMA_URL}/api/generate",
            json={
                "model": MODEL_NAME,
                "prompt": text,
                "stream": False,
                "options": {"temperature": 0.7, "num_predict": 50}
            },
            timeout=180
        )
        data = r.json()
        response = data.get("response", "")
        if not response:
            response = data.get("thinking", "")
        return response[:200] if response else "没有生成回复"
    except Exception as e:
        return f"错误: {e}"

speak("你好，我是叶子")

while True:
    try:
        text = input("\n你说: ").strip()
        if not text:
            continue
        if "退出" in text:
            speak("再见")
            break
        
        print("🤔 思考中...")
        start = time.time()
        reply = ask(text)
        elapsed = time.time() - start
        
        print(f"💭 {reply}")
        if reply and not reply.startswith("错误"):
            speak(reply)
        print(f"⏱️ 耗时: {elapsed:.1f}秒")
        
    except KeyboardInterrupt:
        break
    except Exception as e:
        print(f"错误: {e}")

print("\n已退出")