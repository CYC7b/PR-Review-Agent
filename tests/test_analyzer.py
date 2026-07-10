"""分析器工具测试（SPEC 8.3 / 5.1）。"""

from __future__ import annotations

from app.tools.analyzer_tool import AnalyzerTool


class TestLanguageDetection:
    def test_detect_python(self):
        analyzer = AnalyzerTool()
        files = [{"path": "src/auth.py"}, {"path": "tests/test_auth.py"}]
        assert analyzer.detect_language(files) == ["python"]

    def test_detect_mixed(self):
        analyzer = AnalyzerTool()
        files = [{"path": "src/app.ts"}, {"path": "src/api.py"}, {"path": "src/util.ts"}]
        langs = analyzer.detect_language(files)
        assert "typescript" in langs
        assert "python" in langs
        assert langs[0] == "typescript"  # 数量更多

    def test_detect_no_code(self):
        analyzer = AnalyzerTool()
        files = [{"path": "README.md"}, {"path": "docs/guide.rst"}]
        assert analyzer.detect_language(files) == []


class TestTestCommandDetection:
    def test_python_project(self):
        analyzer = AnalyzerTool()
        cmds = analyzer.detect_test_commands(["src/app.py", "pyproject.toml", "setup.py"])
        cmd_strs = [c["cmd"] for c in cmds]
        assert "python -m pytest -q" in cmd_strs
        assert "ruff check ." in cmd_strs

    def test_node_project(self):
        analyzer = AnalyzerTool()
        cmds = analyzer.detect_test_commands(["package.json", "src/index.js"])
        cmd_strs = [c["cmd"] for c in cmds]
        assert "npm test" in cmd_strs
        assert "npm run lint" in cmd_strs

    def test_go_project(self):
        analyzer = AnalyzerTool()
        cmds = analyzer.detect_test_commands(["go.mod", "main.go"])
        cmd_strs = [c["cmd"] for c in cmds]
        assert "go test ./..." in cmd_strs

    def test_makefile(self):
        analyzer = AnalyzerTool()
        cmds = analyzer.detect_test_commands(["makefile", "main.c"])
        assert any(c["cmd"] == "make test" for c in cmds)

    def test_discovers_package_scripts_from_full_repository_context(self):
        analyzer = AnalyzerTool()
        cmds = analyzer.discover_test_commands(
            ["src/service.ts", "package.json", "pnpm-lock.yaml"],
            {"package.json": '{"scripts":{"test":"vitest","lint":"eslint .","build":"tsc"}}'},
        )
        assert [c["cmd"] for c in cmds] == ["pnpm run test", "pnpm run lint", "pnpm run build"]

    def test_dependency_commands_prefer_lockfile(self):
        analyzer = AnalyzerTool()
        assert analyzer.detect_dependency_commands(["package.json", "package-lock.json"]) == ["npm ci"]


class TestPRClassification:
    def test_docs_only(self, sample_changed_files):
        analyzer = AnalyzerTool()
        files = [{"path": "README.md"}, {"path": "docs/guide.md"}]
        cats = analyzer.classify_pr(files)
        assert cats == ["docs"]
        assert analyzer.is_docs_only(files) is True

    def test_code_and_security(self, sample_changed_files):
        analyzer = AnalyzerTool()
        cats = analyzer.classify_pr(sample_changed_files)
        from app.models import PrChangeCategory
        assert PrChangeCategory.CODE.value in cats
        assert PrChangeCategory.SECURITY_SENSITIVE.value in cats  # auth.py
        assert PrChangeCategory.DEPENDENCY.value in cats  # requirements.txt

    def test_test_files(self):
        analyzer = AnalyzerTool()
        cats = analyzer.classify_pr([{"path": "tests/test_auth.py"}])
        from app.models import PrChangeCategory
        # test files also contain .py so code category included
        assert PrChangeCategory.TEST.value in cats


class TestSecurityScan:
    def test_sql_injection(self, sample_file_contents):
        analyzer = AnalyzerTool()
        findings = analyzer.security_scan(["src/auth.py"], sample_file_contents)
        rules = [f["rule"] for f in findings]
        assert "sql-injection-concat" in rules

    def test_hardcoded_secret(self, sample_file_contents):
        analyzer = AnalyzerTool()
        findings = analyzer.security_scan(["src/auth.py"], sample_file_contents)
        rules = [f["rule"] for f in findings]
        assert "hardcoded-secret" in rules

    def test_clean_code(self):
        analyzer = AnalyzerTool()
        content = {"clean.py": "def add(a, b):\n    return a + b\n"}
        findings = analyzer.security_scan(["clean.py"], content)
        assert findings == []


class TestLint:
    def test_bare_except(self):
        analyzer = AnalyzerTool()
        content = {"app.py": "try:\n    x = 1\nexcept:\n    pass\n"}
        findings = analyzer.lint(["app.py"], "python", content)
        rules = [f["rule"] for f in findings]
        assert "bare-except" in rules

    def test_todo(self):
        analyzer = AnalyzerTool()
        content = {"app.py": "# TODO: fix this\nx = 1\n"}
        findings = analyzer.lint(["app.py"], "python", content)
        assert any(f["rule"] == "todo-fixme" for f in findings)


class TestRiskAssessment:
    def test_high_risk_auth(self, sample_changed_files):
        analyzer = AnalyzerTool()
        results = analyzer.assess_risk(sample_changed_files)
        auth = next(r for r in results if r["path"] == "src/auth.py")
        assert auth["risk_level"] == "high"
        assert "authentication" in auth["reasons"] or any("auth" in r for r in auth["reasons"])

    def test_low_risk_readme(self, sample_changed_files):
        analyzer = AnalyzerTool()
        results = analyzer.assess_risk(sample_changed_files)
        readme = next(r for r in results if r["path"] == "README.md")
        assert readme["risk_level"] == "low"
