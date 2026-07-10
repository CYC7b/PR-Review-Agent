"""Analyzer Tool —— 语言检测、测试命令识别、lint/SAST 模式分析（SPEC 8.3）。

静态分析函数操作文件内容（无需沙箱），并生成需在沙箱中执行的命令。
动态 lint/SAST 实际执行由 TestExecutor 通过 sandbox.exec 完成。
"""

from __future__ import annotations

import json
import re
from typing import Any

# 语言 ↔ 扩展名映射
LANG_BY_EXT = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".go": "go",
}

# 安全敏感路径关键词
SECURITY_SENSITIVE_KEYWORDS = [
    "auth", "login", "password", "passwd", "secret", "token", "crypto",
    "session", "permission", "rbac", "acl", "admin", "jwt", "oauth",
]

# 文档扩展名
DOC_EXTS = {".md", ".rst", ".txt", ".adoc", ".ipynb"}


# 安全风险正则模式（基础 SAST 启发式）
_SECURITY_PATTERNS: list[tuple[str, str, str]] = [
    # (id, description, regex)
    ("sql-injection-concat",
     "SQL 查询使用字符串拼接，可能存在 SQL 注入",
     r'(?i)(?:SELECT|INSERT|UPDATE|DELETE|DROP)\s+.*["\']?\s*(?:\+|%|\.format\(|f["\']|\{|,)\s*\w'),
    ("hardcoded-secret",
     "疑似硬编码密钥/口令",
     r'(?i)(password|passwd|secret|api_key|apikey|token)\s*[:=]\s*["\'][^"\']{6,}["\']|return\s+["\'](?:sk-|ghp_|gho_|AKIA|glpat-|xox[baprs-])[A-Za-z0-9_-]{8,}["\']'),
    ("command-injection",
     "使用 shell=True 或 os.system 拼接不可信输入，可能存在命令注入",
     r'(?i)(os\.system|subprocess\.(?:call|run|Popen|check_output))\s*\([^)]*(?:%|\+|f["\']|format)'),
    ("weak-hash",
     "使用弱哈希算法 MD5/SHA1",
     r'(?i)hashlib\.(md5|sha1)\s*\('),
    ("eval-exec",
     "使用 eval/exec 处理不可信输入",
     r'(?i)\beval\s*\(|\bexec\s*\('),
    ("pickle-load",
     "使用 pickle 反序列化不可信数据",
     r'(?i)pickle\.loads?\s*\('),
    ("ssrf-requests",
     "HTTP 请求目标来自用户输入，可能存在 SSRF",
     r'(?i)requests\.(?:get|post|put|delete|head)\s*\(\s*(?:f["\']|%|\+|format|user)'),
    ("insecure-deserialization",
     "使用不安全的反序列化",
     r'(?i)yaml\.load\s*\(\s*[^)]*(?!Loader)'),
    ("debug-true",
     "DEBUG 模式在生产环境中可能开启",
     r'(?i)DEBUG\s*=\s*True'),
]

# 风格/可维护性正则模式
_STYLE_PATTERNS: list[tuple[str, str, str]] = [
    ("bare-except",
     "使用裸 except，可能吞掉所有异常",
     r'except\s*:'),
    ("todo-fixme",
     "代码中存在 TODO/FIXME",
     r'(?i)#\s*(TODO|FIXME|HACK|XXX)'),
    ("long-line",
     "代码行过长",
     r'^.{120,}$'),
]


class AnalyzerTool:
    """静态分析工具集。"""

    def detect_language(self, changed_files: list[dict[str, Any]]) -> list[str]:
        """analyzer.detect_language —— 根据文件扩展名识别语言画像。"""
        from collections import Counter

        counter: Counter[str] = Counter()
        for f in changed_files:
            path = f.get("path", f) if isinstance(f, dict) else str(f)
            ext = "." + path.rsplit(".", 1)[-1] if "." in path else ""
            lang = LANG_BY_EXT.get(ext.lower())
            if lang:
                counter[lang] += 1
        # 按出现频率排序
        return [lang for lang, _ in counter.most_common()]

    def detect_test_commands(self, repo_files: list[str]) -> list[dict[str, Any]]:
        """analyzer.detect_test_commands —— 识别候选测试/lint 命令（SPEC 5.4）。"""
        file_set = {f.lower() for f in repo_files}
        commands: list[dict[str, Any]] = []

        if "package.json" in file_set:
            commands.extend([
                {"cmd": "npm test", "kind": "test", "source": "package.json"},
                {"cmd": "npm run lint", "kind": "lint", "source": "package.json"},
            ])
        if "pyproject.toml" in file_set or "setup.py" in file_set:
            commands.extend([
                {"cmd": "python -m pytest -q", "kind": "test", "source": "pyproject/setup"},
                {"cmd": "ruff check .", "kind": "lint", "source": "ruff"},
                {"cmd": "mypy .", "kind": "typecheck", "source": "mypy"},
            ])
        if "go.mod" in file_set:
            commands.extend([
                {"cmd": "go test ./...", "kind": "test", "source": "go.mod"},
                {"cmd": "go vet ./...", "kind": "lint", "source": "go vet"},
                {"cmd": "golangci-lint run", "kind": "lint", "source": "golangci-lint"},
            ])
        if "makefile" in file_set:
            commands.append({"cmd": "make test", "kind": "test", "source": "Makefile"})
        if ".github/workflows" in "/".join(repo_files) or any(
            f.startswith(".github/workflows") for f in repo_files
        ):
            commands.append({"cmd": "__CI_CONFIG__", "kind": "test", "source": "ci-config"})

        return commands

    def discover_test_commands(
        self, repo_files: list[str], file_contents: dict[str, str] | None = None
    ) -> list[dict[str, Any]]:
        """Return executable, repo-backed commands without trusting arbitrary CI text."""
        files = {path.lower(): path for path in repo_files}
        contents = file_contents or {}
        commands: list[dict[str, Any]] = []
        seen: set[str] = set()

        def add(command: str, kind: str, source: str) -> None:
            if command not in seen:
                seen.add(command)
                commands.append({"cmd": command, "kind": kind, "source": source})

        package_path = files.get("package.json")
        if package_path:
            package_manager = "pnpm" if "pnpm-lock.yaml" in files else "yarn" if "yarn.lock" in files else "npm"
            scripts: dict[str, Any] = {}
            try:
                scripts = json.loads(contents.get(package_path, "{}")).get("scripts", {}) or {}
            except (ValueError, TypeError):
                pass
            for name, kind in (("test", "test"), ("lint", "lint"), ("typecheck", "typecheck"), ("build", "build")):
                if name in scripts:
                    add(f"{package_manager} run {name}", kind, "package.json")
            if "test" not in scripts and any(re.search(r"(^|/)(test|tests)/.*\.(?:[cm]?[jt]sx?)$", p, re.I) for p in repo_files):
                add("node --test", "test", "node test files")

        is_python = any(path in files for path in ("pyproject.toml", "setup.py", "pytest.ini", "tox.ini"))
        python_tests = any(re.search(r"(^|/)(test|tests)/.*\.py$|(^|/)test_.*\.py$", p, re.I) for p in repo_files)
        if is_python or python_tests:
            add("python -m pytest -q", "test", "python project")
            pyproject = contents.get(files.get("pyproject.toml", ""), "")
            if any(path in files for path in ("ruff.toml", ".ruff.toml")) or "[tool.ruff]" in pyproject:
                add("ruff check .", "lint", "ruff config")
            if "[tool.mypy]" in pyproject or "mypy.ini" in files:
                add("mypy .", "typecheck", "mypy config")

        if "go.mod" in files:
            add("go test ./...", "test", "go.mod")
            add("go vet ./...", "lint", "go.mod")
            if ".golangci.yml" in files or ".golangci.yaml" in files:
                add("golangci-lint run", "lint", "golangci config")

        make_path = files.get("makefile")
        make_text = contents.get(make_path, "") if make_path else ""
        for target, kind in (("test", "test"), ("lint", "lint"), ("check", "typecheck")):
            if re.search(rf"^{target}\s*:", make_text, re.MULTILINE):
                add(f"make {target}", kind, "Makefile")
        return commands

    def detect_dependency_commands(
        self, repo_files: list[str], file_contents: dict[str, str] | None = None
    ) -> list[str]:
        """Choose lockfile-backed installs; never mask an install failure."""
        files = {path.lower(): path for path in repo_files}
        if "pnpm-lock.yaml" in files:
            return ["corepack enable && pnpm install --frozen-lockfile"]
        if "yarn.lock" in files:
            return ["corepack enable && yarn install --frozen-lockfile"]
        if "package-lock.json" in files:
            return ["npm ci"]
        if "package.json" in files:
            return ["npm install"]
        if "requirements.txt" in files:
            return ["pip install -r requirements.txt"]
        if "pyproject.toml" in files or "setup.py" in files:
            return ["pip install -e ."]
        if "go.mod" in files:
            return ["go mod download"]
        return []

    def classify_pr(self, changed_files: list[dict[str, Any]]) -> list[str]:
        """判断 PR 变更类型（SPEC 5.1 第 6 步）。"""
        from app.models import PrChangeCategory

        categories: set[str] = set()
        all_paths = [f["path"] if isinstance(f, dict) else str(f) for f in changed_files]
        if not all_paths:
            return []

        for path in all_paths:
            lower = path.lower()
            ext = "." + lower.rsplit(".", 1)[-1] if "." in lower else ""
            # 依赖清单优先于文档扩展名检查
            if any(name in lower for name in (
                "package.json", "package-lock.json", "requirements.txt", "go.mod", "go.sum",
                "poetry.lock", "yarn.lock", "pnpm-lock.yaml", "setup.py", "setup.cfg",
            )):
                categories.add(PrChangeCategory.DEPENDENCY.value)
            elif ext in DOC_EXTS:
                categories.add(PrChangeCategory.DOCS.value)
            elif any(kw in lower for kw in ("test", "spec", "__tests__")):
                categories.add(PrChangeCategory.TEST.value)
            elif ext in (".yml", ".yaml", ".toml", ".ini", ".env", ".cfg", ".conf", ".json"):
                categories.add(PrChangeCategory.CONFIG.value)
            elif ext in LANG_BY_EXT:
                categories.add(PrChangeCategory.CODE.value)
                if any(kw in lower for kw in SECURITY_SENSITIVE_KEYWORDS):
                    categories.add(PrChangeCategory.SECURITY_SENSITIVE.value)

        # 若仅有文档变更
        if categories == {PrChangeCategory.DOCS.value}:
            return [PrChangeCategory.DOCS.value]
        categories.discard(PrChangeCategory.DOCS.value)
        return sorted(categories)

    def is_docs_only(self, changed_files: list[dict[str, Any]]) -> bool:
        from app.models import PrChangeCategory

        cats = self.classify_pr(changed_files)
        return cats == [PrChangeCategory.DOCS.value]

    def lint(
        self, file_paths: list[str], language: str, file_contents: dict[str, str]
    ) -> list[dict[str, Any]]:
        """analyzer.lint —— 基础风格/可维护性模式扫描。

        返回启发式发现。完整 lint 由 sandbox 执行（ruff/eslint/golangci-lint）。
        """
        findings: list[dict[str, Any]] = []
        for path in file_paths:
            content = file_contents.get(path, "")
            if not content:
                continue
            for line_no, line in enumerate(content.splitlines(), start=1):
                for rule_id, desc, pattern in _STYLE_PATTERNS:
                    if re.search(pattern, line):
                        findings.append({
                            "rule": rule_id,
                            "file": path,
                            "line": line_no,
                            "message": desc,
                            "severity": "low",
                        })
        return findings

    def security_scan(
        self, file_paths: list[str], file_contents: dict[str, str], ruleset: str = "default"
    ) -> list[dict[str, Any]]:
        """analyzer.security_scan —— 基础 SAST 启发式扫描。

        返回启发式发现。完整 SAST 由 sandbox 执行（semgrep/bandit）。
        """
        findings: list[dict[str, Any]] = []
        for path in file_paths:
            content = file_contents.get(path, "")
            if not content:
                continue
            for line_no, line in enumerate(content.splitlines(), start=1):
                for rule_id, desc, pattern in _SECURITY_PATTERNS:
                    if re.search(pattern, line):
                        severity = "high" if rule_id in (
                            "sql-injection-concat", "hardcoded-secret",
                            "command-injection", "insecure-deserialization",
                        ) else "medium"
                        findings.append({
                            "rule": rule_id,
                            "file": path,
                            "line": line_no,
                            "message": desc,
                            "severity": severity,
                        })
        return findings

    def assess_risk(self, changed_files: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """评估每个变更文件的风险等级（SPEC 5.2）。"""
        from app.models import RiskLevel

        results = []
        for f in changed_files:
            path = f["path"] if isinstance(f, dict) else str(f)
            lower = path.lower()
            if any(kw in lower for kw in SECURITY_SENSITIVE_KEYWORDS):
                level = RiskLevel.HIGH
                reasons = [kw for kw in SECURITY_SENSITIVE_KEYWORDS if kw in lower]
            elif any(kw in lower for kw in ("migration", "schema", "model", "router", "view")):
                level = RiskLevel.MEDIUM
                reasons = ["core-logic"]
            else:
                level = RiskLevel.LOW
                reasons = []
            change_type = "modified"
            if isinstance(f, dict):
                status = f.get("status", "modified")
                change_type = status
            results.append({
                "path": path,
                "change_type": change_type,
                "risk_level": level.value,
                "reasons": reasons,
            })
        return results


_default_analyzer: AnalyzerTool | None = None


def get_analyzer() -> AnalyzerTool:
    global _default_analyzer
    if _default_analyzer is None:
        _default_analyzer = AnalyzerTool()
    return _default_analyzer
