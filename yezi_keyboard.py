#!/usr/bin/env python3
"""叶子语音助手 - 键盘输入版"""

import os
import sys
import pyttsx3

print("""
╔═══════════════════════════════════════════════════════════╗
║     🌸 叶子 - 语音助手（键盘输入版）                  ║
║                                                           ║
║     使用方法：                                            ║
║     • 打字输入问题，叶子会语音回复                    ║
║     • 说"叶子"唤醒我                                      ║
║     • 说"换声音"切换声线                                  ║
║     • 说"退出"结束                                        ║
║                                                           ║
╚═══════════════════════════════════════════════════════════╝
""")

# 初始化语音
engine = pyttsx3.init()
voices = engine.getProperty('voices')
if len(voices) > 0:
    engine.setProperty('voice', voices[0].id)
engine.setProperty('rate', 180)

def speak(text):
    print(f"🔊 叶子: {text}")
    engine.say(text)
    engine.runAndWait()

# 启动
speak("你好，我是叶子，现在开始对话")

while True:
    try:
        text = input("\n你说: ")
        
        if not text.strip():
            continue
            
        if "退出" in text or "再见" in text:
            speak("再见，下次再聊")
            break
            
        if "换声音" in text or "切换" in text:
            current = engine.getProperty('voice')
            for v in voices:
                if v.id != current:
                    engine.setProperty('voice', v.id)
                    speak(f"已切换到 {v.name}")
                    break
            continue
            
        # 处理命令
        if "你好" in text or "hi" in text.lower():
            speak("你好呀，有什么可以帮你的")
        elif "几点了" in text or "时间" in text:
            import datetime
            now = datetime.datetime.now().strftime("%H:%M")
            speak(f"现在是 {now}")
        elif "天气" in text:
            speak("天气查询功能还没准备好，你可以自己查一下")
        else:
            # 默认回复
            speak(f"你说的是：{text}，我收到了")

    except KeyboardInterrupt:
        speak("再见")
        break
    except Exception as e:
        print(f"错误: {e}")

print("\n已退出叶子语音助手")
