#!/bin/bash
# 前哨每日信息搜集任务
# 每天早上8:30运行

cd /Users/hangzhou/.openclaw

# 飞书群webhook（与news-pusher同一个群）
FEISHU_WEBHOOK="https://open.feishu.cn/open-apis/bot/v2/hook/4546a240-5dcd-44a4-ab3e-3ebafa9fe3ef"

# 使用openclaw sessions spawn启动scout agent
openclaw sessions spawn \
  --agent scout \
  --task "今天是$(date '+%Y-%m-%d')，请执行每日信息搜集任务：

1. 使用browser工具访问以下信息源：
   - 36氪 (36kr.com) - 科技创业
   - 虎嗅 (huxiu.com) - 科技商业
   - 财新网 (caixin.com) - 财经政治
   - OpenAI官网/博客 - AI进展

2. 搜集以下领域的前沿信息：
   - 科技前沿：新技术、新产品、新突破
   - 经济动态：宏观经济、市场趋势、政策变化
   - 政治要闻：国际关系、地缘政治、重大事件
   - AI进展：模型更新、应用突破、行业动态

3. 对每条信息进行深度分析：
   - 发生了什么？
   - 为什么重要？
   - 对主人有什么影响？

4. 【重要】A股影响分析：
   - 对每条重要新闻，进行逻辑推导式的A股影响分析
   - 不要强关联，要有逻辑链条
   - 示例推导格式：
     新闻事件 → 直接影响链条 → 间接影响链条 → A股相关板块/个股
   - 每一步推导都要有依据
   - 区分短期影响和长期影响

5. 生成结构化报告，使用curl推送到飞书群webhook：
   $FEISHU_WEBHOOK

注意：
- 只保留真正重要的信息，过滤噪音
- 每条信息都要有分析，不只是搬运
- A股影响分析要有逻辑推导，不要强关联
- 如果有需要主人关注或决策的事项，明确指出"