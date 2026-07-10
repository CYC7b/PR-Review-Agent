"""回归测试：对抗性审查修复（C1/C2/H1/H5 等）。"""

from __future__ import annotations

import pytest


class TestSandboxHardening:
    def test_security_run_kwargs_hardened(self):
        """H1：不再 seccomp:unconfined；丢弃所有 capability；非 root；只读 rootfs。"""
        from app.config import get_config
        from app.tools.sandbox_tool import DockerSandboxManager

        # 无需真实 docker：直接调用 kwargs 构造器
        mgr = DockerSandboxManager.__new__(DockerSandboxManager)
        kwargs = mgr._security_run_kwargs(get_config().sandbox, None)

        assert "seccomp:unconfined" not in kwargs["security_opt"]
        assert "no-new-privileges:true" in kwargs["security_opt"]
        assert kwargs["cap_drop"] == ["ALL"]
        assert kwargs["privileged"] is False
        assert kwargs["read_only"] is True
        assert kwargs["user"] == "reviewer"
        assert kwargs["network_mode"] == "none"

    def test_local_backend_no_host_env_leak(self, monkeypatch):
        """C2：本地沙箱不得透传宿主 os.environ（含 token/secret）。"""
        monkeypatch.setenv("GITHUB_TOKEN", "leak_probe_xyz")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "leak_probe_aws")
        from app.tools.sandbox_tool import LocalSandboxManager

        mgr = LocalSandboxManager()
        sid = mgr.create("rev-c2")
        try:
            r = mgr.exec(sid, "env")
            assert "leak_probe_xyz" not in r.stdout
            assert "leak_probe_aws" not in r.stdout
            # 显式非敏感变量可放行，token 命名变量被过滤
            r2 = mgr.exec(sid, "env", env_policy={"MY_FLAG": "1", "GITHUB_TOKEN": "nope"})
            assert "MY_FLAG=1" in r2.stdout
            assert "GITHUB_TOKEN" not in r2.stdout
        finally:
            mgr.destroy(sid)

    def test_docker_unavailable_fails_hard_by_default(self, monkeypatch):
        """C1：Docker 不可用且未启用不安全回退时必须 fail-hard，绝不在宿主执行。"""
        import app.tools.sandbox_tool as sb

        def broken_init(self):
            raise RuntimeError("docker daemon down")

        monkeypatch.setattr(sb.DockerSandboxManager, "__init__", broken_init)
        sb.reset_sandbox_manager()
        try:
            with pytest.raises(RuntimeError):
                sb.get_sandbox_manager()
        finally:
            sb.reset_sandbox_manager()

    def test_dependency_install_fail_closed_without_proxy(self, monkeypatch):
        """C3：未配置 egress 代理时依赖安装 fail-closed（跳过），不放开公网。"""
        from app.tools.sandbox_tool import DockerSandboxManager

        mgr = DockerSandboxManager.__new__(DockerSandboxManager)
        mgr._instances = {}
        from app.tools.sandbox_tool import SandboxInstance

        mgr._instances["s1"] = SandboxInstance(
            sandbox_id="s1", review_id="r", image="img", network_policy="dependency"
        )
        # egress_proxy_url 默认为空
        result = mgr.install_dependencies("s1", ["pip install requests"], network_policy="dependency")
        assert result.get("skipped") is True
        assert result["all_success"] is False


class TestTestExecutorAttribution:
    def _executor(self):
        from app.agents.test_executor import TestExecutor

        return TestExecutor(review_id="r1", head_sha="abc", sandbox_id="s1")

    def test_unattributable_failure_not_marked_pr_introduced(self):
        """H5：失败输出未引用变更文件时，不武断标记 PR 引入，置信度降低。"""
        ex = self._executor()
        result = {"exit_code": 1, "stdout": "some failure in vendor/lib.py", "stderr": ""}
        issue = ex._failure_to_issue("pytest -q", result, [{"path": "src/app.py"}])
        assert issue is not None
        assert issue.introduced_by_pr is False
        assert issue.confidence < 0.5

    def test_changed_file_failure_waits_for_baseline_attribution(self):
        """单次 head 失败不能在没有 base 对比时归因到 PR。"""
        ex = self._executor()
        result = {"exit_code": 1, "stdout": "FAILED src/app.py::test_x", "stderr": ""}
        issue = ex._failure_to_issue("pytest -q", result, [{"path": "src/app.py"}])
        assert issue is not None
        assert issue.introduced_by_pr is False
        assert issue.confidence < 0.5

    def test_dependency_failure_not_reported_as_code_issue(self):
        ex = self._executor()
        result = {"exit_code": 1, "stdout": "ModuleNotFoundError: no module named 'foo'", "stderr": ""}
        issue = ex._failure_to_issue("pytest -q", result, [{"path": "src/app.py"}])
        assert issue is None

    def test_zero_commands_is_incomplete_not_passed(self):
        from app.agents.test_executor import TestExecutor

        class Gateway:
            def call(self, _name, **kwargs):
                if _name == "sandbox.list_files":
                    return ["src/service.py"]
                if _name == "sandbox.read_files":
                    return {}
                raise AssertionError(_name)

        ex = TestExecutor(review_id="r1", head_sha="abc", sandbox_id="s1", gateway=Gateway())
        result = ex.run([{"path": "src/service.py"}])
        assert result["status"] == "incomplete"
        assert result["commands"] == []
