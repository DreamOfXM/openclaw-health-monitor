#!/usr/bin/env python3
"""
Tech Radar - 每周获取 AI 前沿信息
"""

import json
import time
from pathlib import Path
from datetime import datetime

WORKSPACE = Path("/Users/hangzhou/.openclaw/workspace-xiaoyi")
SHARED_CONTEXT = Path("/Users/hangzhou/openclaw-health-monitor/data/shared-context")
MEMORY_DIR = WORKSPACE / "memory"

def fetch_github_trending():
    """获取 GitHub trending（AI 相关）"""
    import subprocess
    
    try:
        # 使用 gh CLI 获取 trending
        result = subprocess.run(
            ["gh", "repo", "list", "--limit", "20", "--json", "name,description,url,stargazerCount"],
            capture_output=True, text=True, timeout=30
        )
        
        if result.returncode != 0:
            return []
        
        repos = json.loads(result.stdout)
        
        # 过滤 AI 相关
        ai_keywords = ["ai", "llm", "agent", "gpt", "claude", "openai", "anthropic", "langchain", "llamaindex"]
        filtered = []
        
        for repo in repos:
            name = repo.get("name", "").lower()
            desc = repo.get("description", "").lower()
            
            if any(kw in name or kw in desc for kw in ai_keywords):
                filtered.append({
                    "name": repo.get("name"),
                    "url": repo.get("url"),
                    "stars": repo.get("stargazerCount"),
                    "description": repo.get("description", "")[:100]
                })
        
        return filtered[:5]
    
    except Exception as e:
        print(f"GitHub trending 获取失败: {e}")
        return []

def fetch_arxiv_papers():
    """获取 arXiv 最新论文（AI 相关）"""
    # 简化版：只返回示例
    # 实际实现需要 arXiv API
    return [
        {
            "title": "示例论文 1",
            "url": "https://arxiv.org/abs/xxx",
            "summary": "这是示例摘要"
        }
    ]

def generate_report(github_repos, arxiv_papers):
    """生成报告"""
    today = datetime.now().strftime("%Y-%m-%d")
    
    report = f"""# Tech Radar - {today}

## GitHub 热门项目（AI 相关）

"""
    
    if github_repos:
        for i, repo in enumerate(github_repos, 1):
            report += f"{i}. **{repo['name']}** ({repo['stars']} ⭐)\n"
            report += f"   - {repo['description']}\n"
            report += f"   - {repo['url']}\n\n"
    else:
        report += "暂无数据\n\n"
    
    report += """## arXiv 最新论文

"""
    
    if arxiv_papers:
        for i, paper in enumerate(arxiv_papers, 1):
            report += f"{i}. **{paper['title']}**\n"
            report += f"   - {paper['summary']}\n"
            report += f"   - {paper['url']}\n\n"
    else:
        report += "暂无数据\n\n"
    
    report += """## 建议关注

- 暂无特别建议

---

**核心原则：信息获取是为了辅助决策，不是为了焦虑。**
"""
    
    return report

def save_report(report):
    """保存报告"""
    today = datetime.now().strftime("%Y-%m-%d")
    
    # 保存到 memory
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    memory_file = MEMORY_DIR / f"{today}-tech-radar.md"
    
    with open(memory_file, "w") as f:
        f.write(report)
    
    print(f"✅ 报告已保存到 {memory_file}")
    
    # 保存结构化数据
    SHARED_CONTEXT.mkdir(parents=True, exist_ok=True)
    tech_radar_file = SHARED_CONTEXT / "tech-radar.json"
    
    data = {
        "date": today,
        "generated_at": int(time.time())
    }
    
    with open(tech_radar_file, "w") as f:
        json.dump(data, f, indent=2)
    
    print(f"✅ 数据已保存到 {tech_radar_file}")

def main():
    print(f"=== Tech Radar - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===")
    
    # 获取 GitHub trending
    print("\n1. 获取 GitHub trending...")
    github_repos = fetch_github_trending()
    print(f"   找到 {len(github_repos)} 个 AI 相关项目")
    
    # 获取 arXiv 论文
    print("\n2. 获取 arXiv 论文...")
    arxiv_papers = fetch_arxiv_papers()
    print(f"   找到 {len(arxiv_papers)} 篇论文")
    
    # 生成报告
    print("\n3. 生成报告...")
    report = generate_report(github_repos, arxiv_papers)
    
    # 保存报告
    print("\n4. 保存报告...")
    save_report(report)
    
    print("\n=== 完成 ===")
    return 0

if __name__ == "__main__":
    exit(main())
