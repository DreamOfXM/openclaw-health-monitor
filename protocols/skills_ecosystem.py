#!/usr/bin/env python3
"""
Skills 生态系统

核心机制：
1. Skill 发布前验证
2. 安全扫描
3. 双渠道发布（GitHub + ClawHub）
4. 版本管理

设计原则：
- 统一 monorepo
- 旧 Skill 冻结只读
- 安全扫描自动检测
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from dataclasses import dataclass, field
from enum import Enum


class SkillStatus(Enum):
    DRAFT = "draft"
    VALIDATED = "validated"
    PUBLISHED = "published"
    FROZEN = "frozen"
    DEPRECATED = "deprecated"


@dataclass
class Skill:
    """Skill 定义"""
    skill_id: str
    name: str
    version: str
    description: str
    author: str
    status: SkillStatus
    created_at: int
    updated_at: int
    files: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "author": self.author,
            "status": self.status.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "files": self.files,
            "dependencies": self.dependencies,
            "metadata": self.metadata,
        }
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Skill":
        return cls(
            skill_id=data["skill_id"],
            name=data["name"],
            version=data["version"],
            description=data["description"],
            author=data["author"],
            status=SkillStatus(data["status"]),
            created_at=data["created_at"],
            updated_at=data["updated_at"],
            files=data.get("files", []),
            dependencies=data.get("dependencies", []),
            metadata=data.get("metadata", {}),
        )


@dataclass
class SecurityScanResult:
    """安全扫描结果"""
    passed: bool
    issues: list[dict[str, Any]]
    scanned_at: int
    scanned_files: int
    
    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "issues": self.issues,
            "scanned_at": self.scanned_at,
            "scanned_files": self.scanned_files,
        }


class SkillsEcosystem:
    """Skills 生态管理器"""
    
    # 安全扫描规则
    SENSITIVE_PATTERNS = [
        r"sk-[a-zA-Z0-9]{20,}",  # API keys
        r"api[_-]?key\s*=\s*['\"][^'\"]+['\"]",  # API key assignments
        r"password\s*=\s*['\"][^'\"]+['\"]",  # Passwords
        r"secret\s*=\s*['\"][^'\"]+['\"]",  # Secrets
        r"token\s*=\s*['\"][^'\"]+['\"]",  # Tokens
        r"/Users/[^/]+/",  # Absolute user paths
        r"/home/[^/]+/",  # Linux user paths
        r"C:\\Users\\[^\\]+\\",  # Windows user paths
    ]
    
    def __init__(self, base_dir: Path):
        self.base_dir = base_dir
        self.skills_dir = base_dir / "skills"
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        
        self.registry_file = self.skills_dir / "registry.json"
        self.scan_results_dir = self.skills_dir / "scan-results"
        self.scan_results_dir.mkdir(parents=True, exist_ok=True)
        
        self.monorepo_dir = base_dir / "skills-monorepo"
        self.clawhub_dir = base_dir / "clawhub-cache"
    
    def create_skill(
        self,
        name: str,
        description: str,
        author: str,
        files: list[str],
        dependencies: Optional[list[str]] = None,
    ) -> Skill:
        """创建新 Skill"""
        skill_id = f"skill-{int(time.time())}-{name[:8]}"
        now = int(time.time())
        
        skill = Skill(
            skill_id=skill_id,
            name=name,
            version="1.0.0",
            description=description,
            author=author,
            status=SkillStatus.DRAFT,
            created_at=now,
            updated_at=now,
            files=files,
            dependencies=dependencies or [],
        )
        
        self._register_skill(skill)
        return skill
    
    def validate_skill(self, skill_id: str) -> tuple[bool, list[str]]:
        """验证 Skill"""
        skill = self._get_skill(skill_id)
        if not skill:
            return False, ["Skill not found"]
        
        errors = []
        
        # 检查必要文件
        required_files = ["SKILL.md", "skill.json"]
        for req_file in required_files:
            found = any(req_file in f for f in skill.files)
            if not found:
                errors.append(f"Missing required file: {req_file}")
        
        # 检查依赖
        for dep in skill.dependencies:
            if not self._check_dependency(dep):
                errors.append(f"Dependency not found: {dep}")
        
        # 检查版本格式
        if not re.match(r"\d+\.\d+\.\d+", skill.version):
            errors.append(f"Invalid version format: {skill.version}")
        
        if errors:
            return False, errors
        
        # 更新状态
        skill.status = SkillStatus.VALIDATED
        skill.updated_at = int(time.time())
        self._update_skill(skill)
        
        return True, []
    
    def security_scan(self, skill_id: str) -> SecurityScanResult:
        """安全扫描"""
        skill = self._get_skill(skill_id)
        if not skill:
            return SecurityScanResult(
                passed=False,
                issues=[{"error": "Skill not found"}],
                scanned_at=int(time.time()),
                scanned_files=0,
            )
        
        issues = []
        scanned_files = 0
        
        for file_path in skill.files:
            path = Path(file_path)
            if not path.exists():
                continue
            
            scanned_files += 1
            content = path.read_text(encoding="utf-8", errors="ignore")
            
            for pattern in self.SENSITIVE_PATTERNS:
                matches = re.findall(pattern, content, re.IGNORECASE)
                if matches:
                    issues.append({
                        "file": str(path),
                        "pattern": pattern,
                        "matches": matches[:3],  # 只显示前3个匹配
                        "severity": "high",
                    })
        
        result = SecurityScanResult(
            passed=len(issues) == 0,
            issues=issues,
            scanned_at=int(time.time()),
            scanned_files=scanned_files,
        )
        
        # 保存扫描结果
        result_file = self.scan_results_dir / f"{skill_id}.json"
        result_file.write_text(
            json.dumps(result.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        
        return result
    
    def publish_to_github(
        self,
        skill_id: str,
        repo_url: str,
        branch: str = "main",
    ) -> bool:
        """发布到 GitHub"""
        skill = self._get_skill(skill_id)
        if not skill or skill.status != SkillStatus.VALIDATED:
            return False
        
        # 安全扫描
        scan_result = self.security_scan(skill_id)
        if not scan_result.passed:
            return False
        
        # 创建 monorepo 目录
        skill_dir = self.monorepo_dir / skill.name
        skill_dir.mkdir(parents=True, exist_ok=True)
        
        # 复制文件
        for file_path in skill.files:
            src = Path(file_path)
            if src.exists():
                dst = skill_dir / src.name
                dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        
        # 写入 skill.json
        skill_json = skill_dir / "skill.json"
        skill_json.write_text(
            json.dumps(skill.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        
        # Git 操作
        try:
            os.chdir(self.monorepo_dir)
            subprocess.run(["git", "init"], check=True, capture_output=True)
            subprocess.run(["git", "add", "."], check=True, capture_output=True)
            subprocess.run(
                ["git", "commit", "-m", f"Publish skill: {skill.name} v{skill.version}"],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "remote", "add", "origin", repo_url],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "push", "-u", "origin", branch],
                check=True,
                capture_output=True,
            )
        except subprocess.CalledProcessError:
            return False
        
        # 更新状态
        skill.status = SkillStatus.PUBLISHED
        skill.updated_at = int(time.time())
        skill.metadata["github_url"] = repo_url
        self._update_skill(skill)
        
        return True
    
    def publish_to_clawhub(
        self,
        skill_id: str,
        clawhub_url: str = "https://clawhub.com",
    ) -> bool:
        """发布到 ClawHub"""
        skill = self._get_skill(skill_id)
        if not skill or skill.status != SkillStatus.PUBLISHED:
            return False
        
        # 模拟发布到 ClawHub
        # 实际实现需要 ClawHub API
        clawhub_cache = self.clawhub_dir / f"{skill.name}.json"
        clawhub_cache.parent.mkdir(parents=True, exist_ok=True)
        
        skill.metadata["clawhub_url"] = f"{clawhub_url}/skills/{skill.name}"
        skill.metadata["clawhub_published_at"] = int(time.time())
        
        clawhub_cache.write_text(
            json.dumps(skill.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        
        self._update_skill(skill)
        return True
    
    def freeze_skill(self, skill_id: str) -> bool:
        """冻结 Skill（只读）"""
        skill = self._get_skill(skill_id)
        if not skill:
            return False
        
        skill.status = SkillStatus.FROZEN
        skill.updated_at = int(time.time())
        self._update_skill(skill)
        
        return True
    
    def deprecate_skill(self, skill_id: str, reason: str) -> bool:
        """废弃 Skill"""
        skill = self._get_skill(skill_id)
        if not skill:
            return False
        
        skill.status = SkillStatus.DEPRECATED
        skill.updated_at = int(time.time())
        skill.metadata["deprecation_reason"] = reason
        self._update_skill(skill)
        
        return True
    
    def list_skills(
        self,
        status: Optional[SkillStatus] = None,
        author: Optional[str] = None,
    ) -> list[Skill]:
        """列出 Skills"""
        skills = []
        
        if not self.registry_file.exists():
            return skills
        
        for line in self.registry_file.read_text(encoding="utf-8").strip().split("\n"):
            if not line:
                continue
            try:
                skill = Skill.from_dict(json.loads(line))
                if status and skill.status != status:
                    continue
                if author and skill.author != author:
                    continue
                skills.append(skill)
            except Exception:
                continue
        
        return skills
    
    def get_skill(self, skill_id: str) -> Optional[Skill]:
        """获取 Skill"""
        return self._get_skill(skill_id)
    
    def _register_skill(self, skill: Skill) -> None:
        """注册 Skill"""
        with open(self.registry_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(skill.to_dict(), ensure_ascii=False) + "\n")
    
    def _update_skill(self, skill: Skill) -> None:
        """更新 Skill"""
        if not self.registry_file.exists():
            return
        
        lines = []
        for line in self.registry_file.read_text(encoding="utf-8").strip().split("\n"):
            if not line:
                continue
            try:
                data = json.loads(line)
                if data.get("skill_id") == skill.skill_id:
                    lines.append(json.dumps(skill.to_dict(), ensure_ascii=False))
                else:
                    lines.append(line)
            except Exception:
                lines.append(line)
        
        self.registry_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    
    def _get_skill(self, skill_id: str) -> Optional[Skill]:
        """获取 Skill"""
        if not self.registry_file.exists():
            return None
        
        for line in self.registry_file.read_text(encoding="utf-8").strip().split("\n"):
            if not line:
                continue
            try:
                skill = Skill.from_dict(json.loads(line))
                if skill.skill_id == skill_id:
                    return skill
            except Exception:
                continue
        
        return None
    
    def _check_dependency(self, dep: str) -> bool:
        """检查依赖是否存在"""
        # 简单实现：检查是否在注册表中
        if not self.registry_file.exists():
            return False
        
        for line in self.registry_file.read_text(encoding="utf-8").strip().split("\n"):
            if not line:
                continue
            try:
                data = json.loads(line)
                if data.get("name") == dep or data.get("skill_id") == dep:
                    return True
            except Exception:
                continue
        
        return False