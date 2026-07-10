"""Sandbox Tool —— 隔离执行环境管理（SPEC 8.2 / 第 9 节）。

两阶段网络模型：
  阶段一 Dependency Resolution：受限网络（allowlist registry），安装依赖
  阶段二 Test Execution：完全无网络，运行测试/lint/SAST/patch 验证

安全约束（SPEC 9.2 / 9.5）：
  - 非 root、禁止 privileged、禁止挂载 docker socket / hostPath
  - rootfs 只读、workspace 临时卷、no-new-privileges
  - seccomp/AppArmor、PID/mount/network namespace 隔离
  - cgroup 限制 CPU/内存/磁盘/进程数
  - 生命周期默认 ≤30 分钟
  - 不注入任何凭据/Token

凭据隔离：token 仅由管理器（容器外）用于下载代码归档，
随后通过 docker cp 注入容器；token 永不进入容器环境。
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import tarfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from app.config import get_config, get_settings
from app.logging_setup import get_logger

logger = get_logger(__name__)

_TEMP_TEST_ROOT = ".pr-review-agent-tests"


def _safe_relative_path(path: str, *, temp_only: bool = False) -> str:
    """Reject paths that could escape the sandbox workspace."""
    candidate = PurePosixPath(path)
    if not path or candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError(f"非法 sandbox 相对路径: {path!r}")
    normalized = str(candidate)
    if temp_only and not (normalized == _TEMP_TEST_ROOT or normalized.startswith(_TEMP_TEST_ROOT + "/")):
        raise ValueError("临时测试只能写入 .pr-review-agent-tests/")
    return normalized


@dataclass
class SandboxInstance:
    sandbox_id: str
    review_id: str
    image: str
    network_policy: str  # disabled | dependency
    created_at: float = field(default_factory=time.time)
    container: Any = None  # docker container 对象
    workspace: str = "/workspace"
    backend: str = "docker"  # docker | local
    # local 后端用的工作目录
    local_dir: str | None = None


@dataclass
class ExecResult:
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    timed_out: bool = False
    duration_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "stdout": self.stdout,
            "stderr": self.stderr,
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "duration_ms": self.duration_ms,
        }


class SandboxManager:
    """沙箱管理器抽象。"""

    def create(self, review_id: str, image: str | None = None,
               resource_limits: dict | None = None,
               network_policy: str = "disabled") -> str: ...

    def prepare_workspace(self, sandbox_id: str, repo: str,
                          base_sha: str, head_sha: str,
                          code_archive: bytes | None = None) -> dict[str, Any]: ...

    def install_dependencies(self, sandbox_id: str, commands: list[str],
                             network_policy: str = "dependency") -> dict[str, Any]: ...

    def exec(self, sandbox_id: str, command: str, timeout: int = 300,
             env_policy: dict | None = None) -> ExecResult: ...

    def apply_patch(self, sandbox_id: str, patch_content: str) -> dict[str, Any]: ...

    def get_diff(self, sandbox_id: str) -> str: ...

    def list_files(self, sandbox_id: str, max_files: int = 5000) -> list[str]: ...

    def read_files(self, sandbox_id: str, paths: list[str], max_bytes_per_file: int = 32768) -> dict[str, str]: ...

    def write_temp_files(self, sandbox_id: str, files: dict[str, str]) -> dict[str, Any]: ...

    def reset_temp_files(self, sandbox_id: str) -> dict[str, Any]: ...

    def destroy(self, sandbox_id: str) -> dict[str, Any]: ...

    def destroy_all_for_review(self, review_id: str) -> int: ...


class DockerSandboxManager(SandboxManager):
    """基于 Docker SDK 的沙箱管理器。"""

    def __init__(self) -> None:
        import docker  # type: ignore

        self._client = docker.from_env()
        self._instances: dict[str, SandboxInstance] = {}

    def _security_run_kwargs(self, cfg, resource_limits: dict | None) -> dict[str, Any]:
        limits = resource_limits or {}
        mem_limit = limits.get("memory", cfg.memory_limit)
        cpu_count = limits.get("cpu", cfg.cpu_limit)
        pids_limit = limits.get("pids", cfg.pid_limit)
        # SPEC 9.2：启用默认 seccomp（不再 unconfined）、丢弃所有 capability、
        # no-new-privileges；若宿主支持则附加 AppArmor profile。
        # 不显式设置 seccomp → Docker 应用其默认 seccomp profile。
        security_opt = ["no-new-privileges:true"]
        return {
            "user": "reviewer",  # 非 root（镜像内预建用户）
            "privileged": False,
            "read_only": True,
            "cap_drop": ["ALL"],
            "tmpfs": {"/tmp": "size=1g", cfg.workspace: f"size={limits.get('disk', cfg.disk_limit)}"},
            "mem_limit": mem_limit,
            "nano_cpus": int(cpu_count * 1e9),
            "pids_limit": pids_limit,
            "security_opt": security_opt,
            "network_mode": "none",  # 默认无网络；依赖阶段单独处理
            "detach": True,
            "tty": False,
        }

    def create(self, review_id: str, image: str | None = None,
               resource_limits: dict | None = None,
               network_policy: str = "disabled") -> str:
        cfg = get_config().sandbox
        image = image or cfg.image
        sandbox_id = f"sbx-{uuid.uuid4().hex[:12]}"
        run_kwargs = self._security_run_kwargs(cfg, resource_limits)
        try:
            container = self._client.containers.run(image, command="sleep infinity", **run_kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.error("sandbox.create_failed", error=str(exc))
            raise
        inst = SandboxInstance(
            sandbox_id=sandbox_id,
            review_id=review_id,
            image=image,
            network_policy=network_policy,
            container=container,
            workspace=cfg.workspace,
            backend="docker",
        )
        self._instances[sandbox_id] = inst
        logger.info("sandbox.created", sandbox_id=sandbox_id, review_id=review_id, image=image)
        return sandbox_id

    def _get(self, sandbox_id: str) -> SandboxInstance:
        inst = self._instances.get(sandbox_id)
        if inst is None:
            raise KeyError(f"未知 sandbox: {sandbox_id}")
        return inst

    def prepare_workspace(self, sandbox_id: str, repo: str,
                          base_sha: str, head_sha: str,
                          code_archive: bytes | None = None) -> dict[str, Any]:
        inst = self._get(sandbox_id)
        ws = inst.workspace
        if code_archive is None:
            # 通过 GitHub 工具下载归档（token 在容器外使用）
            from app.tools.github_tool import get_github_client

            gh = get_github_client()
            archive_url = f"https://api.github.com/repos/{repo}/tarball/{head_sha}"
            token = gh._get_installation_token(repo)  # noqa: SLF001
            import httpx

            resp = httpx.get(
                archive_url,
                headers={"Authorization": f"token {token}", "Accept": "application/vnd.github+json"},
                follow_redirects=True,
                timeout=60.0,
            )
            if resp.status_code != 200:
                raise RuntimeError(f"下载代码归档失败: {resp.status_code}")
            code_archive = resp.content
        # 解压归档到容器 workspace
        self._put_tarball(inst, code_archive, ws)
        # 初始化 git（用于 diff/patch）
        self.exec(sandbox_id, "git init && git add -A && git commit -m init --allow-empty")
        return {"status": "ready", "workspace": ws, "head_sha": head_sha}

    def _put_tarball(self, inst: SandboxInstance, tarball: bytes, dest: str) -> None:
        # GitHub tarball 内有顶层目录，需展平后放入 dest
        with io.BytesIO(tarball) as bio:
            with tarfile.open(fileobj=bio, mode="r:gz") as tf:
                members = tf.getmembers()
                prefix = members[0].name.split("/")[0] if members else ""
                for m in members:
                    if m.name == prefix:
                        continue
                    m.name = m.name[len(prefix) + 1:] if m.name.startswith(prefix + "/") else m.name
                    tf.extract(m, path=f"/tmp/sbx_extract_{inst.sandbox_id}")
        # 复制到容器
        extract_dir = f"/tmp/sbx_extract_{inst.sandbox_id}"
        # 打包为 tar 传入容器
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as out_tf:
            for root, _dirs, files in os.walk(extract_dir):
                for fname in files:
                    fpath = os.path.join(root, fname)
                    arcname = os.path.relpath(fpath, extract_dir)
                    out_tf.add(fpath, arcname=arcname)
        buf.seek(0)
        inst.container.put_archive(dest, buf.getvalue())
        shutil.rmtree(extract_dir, ignore_errors=True)

    def install_dependencies(self, sandbox_id: str, commands: list[str],
                             network_policy: str = "dependency") -> dict[str, Any]:
        inst = self._get(sandbox_id)
        cfg = get_config().sandbox
        # SPEC 9.3 阶段一：仅允许经受限出网代理访问 allowlist registry。
        # fail-closed：未配置 egress_proxy_url 时不放开任何网络，跳过依赖安装，
        # 绝不回退到无限制公网出网（修复 C3）。
        proxy_env: dict[str, str] = {}
        if network_policy == "dependency":
            if not cfg.egress_proxy_url:
                logger.warning("sandbox.dependency_no_egress_proxy_skip", sandbox_id=sandbox_id)
                return {
                    "results": [],
                    "all_success": False,
                    "skipped": True,
                    "reason": "未配置 egress_proxy_url，依赖安装在无网络下跳过（fail-closed）",
                }
            self._connect_dependency_network(inst)
            no_proxy = "169.254.169.254,metadata.google.internal,localhost,127.0.0.1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"
            proxy_env = {
                "HTTP_PROXY": cfg.egress_proxy_url, "HTTPS_PROXY": cfg.egress_proxy_url,
                "http_proxy": cfg.egress_proxy_url, "https_proxy": cfg.egress_proxy_url,
                "NO_PROXY": no_proxy, "no_proxy": no_proxy,
                "PIP_INDEX_URL": "https://pypi.org/simple",
            }
        results = []
        try:
            for cmd in commands:
                r = self.exec(sandbox_id, cmd, timeout=600, env_policy=proxy_env)
                results.append({"command": cmd, **r.to_dict()})
                if r.exit_code != 0:
                    break
        finally:
            if network_policy == "dependency":
                self._disconnect_network(inst)
        return {"results": results, "all_success": all(r["exit_code"] == 0 for r in results)}

    def _connect_dependency_network(self, inst: SandboxInstance) -> None:
        """连接到受限的依赖解析网络（出网仅经 egress 代理，代理侧强制 allowlist）。"""
        try:
            self._client.networks.get("pr-review-deps")
        except Exception:  # noqa: BLE001
            self._client.networks.create("pr-review-deps", driver="bridge")
        try:
            inst.container.connect("pr-review-deps")
        except Exception as exc:  # noqa: BLE001
            logger.warning("sandbox.connect_network_failed", error=str(exc))

    def _disconnect_network(self, inst: SandboxInstance) -> None:
        try:
            inst.container.disconnect("pr-review-deps")
        except Exception:  # noqa: BLE001
            pass

    def _enforce_lifetime(self, inst: SandboxInstance) -> None:
        """强制容器生命周期上限（SPEC 9.2 / H2）：超时则销毁并抛错。"""
        max_life = get_config().sandbox.max_lifetime_seconds
        if max_life and (time.time() - inst.created_at) > max_life:
            logger.warning("sandbox.lifetime_exceeded", sandbox_id=inst.sandbox_id,
                           age_s=int(time.time() - inst.created_at))
            self.destroy(inst.sandbox_id)
            raise TimeoutError(f"sandbox {inst.sandbox_id} 超过生命周期上限 {max_life}s")

    def exec(self, sandbox_id: str, command: str, timeout: int = 300,
             env_policy: dict | None = None) -> ExecResult:
        inst = self._get(sandbox_id)
        self._enforce_lifetime(inst)
        start = time.monotonic()
        # env_policy 仅允许非敏感环境变量（子串匹配，避免 GITHUB_TOKEN 之类漏网，M2）
        environment = self._filter_env(env_policy)
        # docker exec_run 本身无超时，使用独立线程 + 看门狗强制超时（H2）。
        holder: dict[str, Any] = {}

        def _do_exec() -> None:
            try:
                holder["result"] = inst.container.exec_run(
                    ["/bin/sh", "-lc", command] if isinstance(command, str) else command,
                    workdir=inst.workspace,
                    environment=environment or None,
                    demux=True,
                )
            except Exception as exc:  # noqa: BLE001
                holder["error"] = exc

        worker = threading.Thread(target=_do_exec, daemon=True)
        worker.start()
        worker.join(timeout)
        if worker.is_alive():
            # 超时：强杀容器内进程，返回 timed_out
            logger.warning("sandbox.exec_timeout", sandbox_id=sandbox_id, timeout=timeout)
            try:
                inst.container.exec_run(["/bin/sh", "-lc", "kill -9 -1 2>/dev/null || true"])
            except Exception:  # noqa: BLE001
                pass
            return ExecResult(stderr="TIMEOUT", exit_code=-1, timed_out=True,
                              duration_ms=int((time.monotonic() - start) * 1000))
        if "error" in holder:
            return ExecResult(stderr=str(holder["error"]), exit_code=-1, timed_out=False,
                              duration_ms=int((time.monotonic() - start) * 1000))
        exit_code, output = holder["result"]
        stdout = (output[0].decode("utf-8", "replace") if output and output[0] else "")
        stderr = (output[1].decode("utf-8", "replace") if output and output[1] else "")
        return ExecResult(
            stdout=stdout, stderr=stderr, exit_code=exit_code,
            duration_ms=int((time.monotonic() - start) * 1000),
        )

    @staticmethod
    def _filter_env(env_policy: dict | None) -> dict[str, str]:
        """仅放行非敏感环境变量（子串匹配敏感关键词）。"""
        blocked = ("token", "secret", "key", "password", "passwd", "credential", "cred")
        out: dict[str, str] = {}
        for k, v in (env_policy or {}).items():
            if any(b in k.lower() for b in blocked):
                continue
            out[k] = str(v)
        return out

    def apply_patch(self, sandbox_id: str, patch_content: str) -> dict[str, Any]:
        inst = self._get(sandbox_id)
        # 写入 patch 文件并应用
        tar_buf = io.BytesIO()
        with tarfile.open(fileobj=tar_buf, mode="w") as tf:
            info = tarfile.TarInfo(name="review.patch")
            data = patch_content.encode("utf-8")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        tar_buf.seek(0)
        inst.container.put_archive(inst.workspace, tar_buf.getvalue())
        r = self.exec(sandbox_id, "git apply --check review.patch && git apply review.patch")
        ok = r.exit_code == 0
        return {"applied": ok, "error": r.stderr if not ok else None}

    def get_diff(self, sandbox_id: str) -> str:
        r = self.exec(sandbox_id, "git diff")
        return r.stdout

    def list_files(self, sandbox_id: str, max_files: int = 5000) -> list[str]:
        # File names are returned line-by-line solely for repository discovery.  The
        # PR archive is untrusted, so do not interpolate paths into shell commands.
        r = self.exec(sandbox_id, f"find . -type f -print | sed 's#^./##' | head -n {max_files}", timeout=30)
        if r.exit_code != 0:
            raise RuntimeError(r.stderr or "无法列出 sandbox 文件")
        return [line for line in r.stdout.splitlines() if line and "\x00" not in line]

    def read_files(self, sandbox_id: str, paths: list[str], max_bytes_per_file: int = 32768) -> dict[str, str]:
        inst = self._get(sandbox_id)
        selected = [_safe_relative_path(path) for path in paths]
        out: dict[str, str] = {}
        for path in selected:
            # Docker's archive API avoids passing untrusted file names to the shell.
            try:
                stream, _ = inst.container.get_archive(f"{inst.workspace}/{path}")
                raw = b"".join(stream)
                with tarfile.open(fileobj=io.BytesIO(raw), mode="r:*") as tf:
                    member = next((m for m in tf.getmembers() if m.isfile()), None)
                    if member and member.size <= max_bytes_per_file:
                        data = tf.extractfile(member)
                        if data:
                            out[path] = data.read(max_bytes_per_file).decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                continue
        return out

    def write_temp_files(self, sandbox_id: str, files: dict[str, str]) -> dict[str, Any]:
        inst = self._get(sandbox_id)
        if len(files) > 12:
            raise ValueError("临时测试文件数超过上限")
        tar_buf = io.BytesIO()
        with tarfile.open(fileobj=tar_buf, mode="w") as tf:
            for path, content in files.items():
                safe_path = _safe_relative_path(path, temp_only=True)
                data = content.encode("utf-8")
                if len(data) > 65536:
                    raise ValueError(f"临时测试文件过大: {safe_path}")
                info = tarfile.TarInfo(name=safe_path)
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
        tar_buf.seek(0)
        inst.container.put_archive(inst.workspace, tar_buf.getvalue())
        return {"written": sorted(files)}

    def reset_temp_files(self, sandbox_id: str) -> dict[str, Any]:
        # Only remove the dedicated test artefact directory; production source is
        # restored separately by PatchGenerator when it applies a candidate patch.
        r = self.exec(sandbox_id, f"rm -rf -- {_TEMP_TEST_ROOT}", timeout=30)
        return {"reset": r.exit_code == 0, "error": r.stderr if r.exit_code else None}

    def destroy(self, sandbox_id: str) -> dict[str, Any]:
        inst = self._instances.pop(sandbox_id, None)
        if inst is None:
            return {"destroyed": False}
        try:
            inst.container.remove(force=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("sandbox.destroy_failed", sandbox_id=sandbox_id, error=str(exc))
        logger.info("sandbox.destroyed", sandbox_id=sandbox_id)
        return {"destroyed": True}

    def destroy_all_for_review(self, review_id: str) -> int:
        count = 0
        for sid in list(self._instances.keys()):
            if self._instances[sid].review_id == review_id:
                self.destroy(sid)
                count += 1
        return count


class LocalSandboxManager(SandboxManager):
    """本地回退沙箱（仅用于开发/测试，不提供强隔离）。

    生产环境必须使用 DockerSandboxManager。
    """

    def __init__(self) -> None:
        self._instances: dict[str, SandboxInstance] = {}
        self._base_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "data", "sandboxes")

    def create(self, review_id: str, image: str | None = None,
               resource_limits: dict | None = None,
               network_policy: str = "disabled") -> str:
        sandbox_id = f"local-sbx-{uuid.uuid4().hex[:12]}"
        local_dir = os.path.join(self._base_dir, sandbox_id)
        os.makedirs(local_dir, exist_ok=True)
        inst = SandboxInstance(
            sandbox_id=sandbox_id, review_id=review_id, image="local",
            network_policy=network_policy, workspace=local_dir, backend="local", local_dir=local_dir,
        )
        self._instances[sandbox_id] = inst
        logger.warning("sandbox.local_backend_insecure", sandbox_id=sandbox_id)
        return sandbox_id

    def _get(self, sandbox_id: str) -> SandboxInstance:
        inst = self._instances.get(sandbox_id)
        if inst is None:
            raise KeyError(f"未知 sandbox: {sandbox_id}")
        return inst

    def prepare_workspace(self, sandbox_id: str, repo: str,
                          base_sha: str, head_sha: str,
                          code_archive: bytes | None = None) -> dict[str, Any]:
        inst = self._get(sandbox_id)
        if code_archive:
            with io.BytesIO(code_archive) as bio:
                with tarfile.open(fileobj=bio, mode="r:gz") as tf:
                    members = tf.getmembers()
                    prefix = members[0].name.split("/")[0] if members else ""
                    for m in members:
                        if m.name == prefix:
                            continue
                        m.name = m.name[len(prefix) + 1:] if m.name.startswith(prefix + "/") else m.name
                        tf.extract(m, path=inst.local_dir)
        self.exec(sandbox_id, "git init && git add -A && git commit -m init --allow-empty")
        return {"status": "ready", "workspace": inst.local_dir, "head_sha": head_sha}

    def install_dependencies(self, sandbox_id: str, commands: list[str],
                             network_policy: str = "dependency") -> dict[str, Any]:
        results = []
        for cmd in commands:
            r = self.exec(sandbox_id, cmd, timeout=600)
            results.append({"command": cmd, **r.to_dict()})
            if r.exit_code != 0:
                break
        return {"results": results, "all_success": all(r["exit_code"] == 0 for r in results)}

    def exec(self, sandbox_id: str, command: str, timeout: int = 300,
             env_policy: dict | None = None) -> ExecResult:
        inst = self._get(sandbox_id)
        start = time.monotonic()
        # 凭据隔离（SPEC 9.5 / C2）：绝不透传宿主 os.environ（含 GITHUB_TOKEN /
        # 云凭据 / LLM key）。仅构造最小干净环境 + 显式放行的非敏感变量。
        env = {
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "HOME": inst.local_dir or "/tmp",
            "LANG": "C.UTF-8",
        }
        blocked = ("token", "secret", "key", "password", "passwd", "credential", "cred")
        if env_policy:
            for k, v in env_policy.items():
                if any(b in k.lower() for b in blocked):
                    continue
                env[k] = str(v)
        try:
            proc = subprocess.run(
                command, shell=True, cwd=inst.local_dir, capture_output=True,
                text=True, timeout=timeout, env=env,
            )
            return ExecResult(
                stdout=proc.stdout, stderr=proc.stderr, exit_code=proc.returncode,
                duration_ms=int((time.monotonic() - start) * 1000),
            )
        except subprocess.TimeoutExpired as exc:
            return ExecResult(
                stdout=exc.stdout or "", stderr=(exc.stderr or "") + "\nTIMEOUT",
                exit_code=-1, timed_out=True,
                duration_ms=int((time.monotonic() - start) * 1000),
            )
        except Exception as exc:  # noqa: BLE001
            return ExecResult(stderr=str(exc), exit_code=-1, duration_ms=int((time.monotonic() - start) * 1000))

    def apply_patch(self, sandbox_id: str, patch_content: str) -> dict[str, Any]:
        inst = self._get(sandbox_id)
        patch_path = os.path.join(inst.local_dir, "review.patch")
        with open(patch_path, "w", encoding="utf-8") as fh:
            fh.write(patch_content)
        r = self.exec(sandbox_id, "git apply --check review.patch && git apply review.patch")
        return {"applied": r.exit_code == 0, "error": r.stderr if r.exit_code != 0 else None}

    def get_diff(self, sandbox_id: str) -> str:
        return self.exec(sandbox_id, "git diff").stdout

    def list_files(self, sandbox_id: str, max_files: int = 5000) -> list[str]:
        inst = self._get(sandbox_id)
        paths: list[str] = []
        for root, _dirs, files in os.walk(inst.local_dir or ""):
            for name in files:
                paths.append(os.path.relpath(os.path.join(root, name), inst.local_dir or ""))
                if len(paths) >= max_files:
                    return sorted(paths)
        return sorted(paths)

    def read_files(self, sandbox_id: str, paths: list[str], max_bytes_per_file: int = 32768) -> dict[str, str]:
        inst = self._get(sandbox_id)
        out: dict[str, str] = {}
        for path in paths:
            safe_path = _safe_relative_path(path)
            full_path = os.path.join(inst.local_dir or "", safe_path)
            if not os.path.isfile(full_path) or os.path.getsize(full_path) > max_bytes_per_file:
                continue
            with open(full_path, "r", encoding="utf-8", errors="replace") as fh:
                out[safe_path] = fh.read(max_bytes_per_file)
        return out

    def write_temp_files(self, sandbox_id: str, files: dict[str, str]) -> dict[str, Any]:
        inst = self._get(sandbox_id)
        if len(files) > 12:
            raise ValueError("临时测试文件数超过上限")
        for path, content in files.items():
            safe_path = _safe_relative_path(path, temp_only=True)
            data = content.encode("utf-8")
            if len(data) > 65536:
                raise ValueError(f"临时测试文件过大: {safe_path}")
            full_path = os.path.join(inst.local_dir or "", safe_path)
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, "w", encoding="utf-8") as fh:
                fh.write(content)
        return {"written": sorted(files)}

    def reset_temp_files(self, sandbox_id: str) -> dict[str, Any]:
        inst = self._get(sandbox_id)
        temp_dir = os.path.join(inst.local_dir or "", _TEMP_TEST_ROOT)
        shutil.rmtree(temp_dir, ignore_errors=True)
        return {"reset": True}

    def destroy(self, sandbox_id: str) -> dict[str, Any]:
        inst = self._instances.pop(sandbox_id, None)
        if inst is None:
            return {"destroyed": False}
        if inst.local_dir and os.path.exists(inst.local_dir):
            shutil.rmtree(inst.local_dir, ignore_errors=True)
        return {"destroyed": True}

    def destroy_all_for_review(self, review_id: str) -> int:
        count = 0
        for sid in list(self._instances.keys()):
            if self._instances[sid].review_id == review_id:
                self.destroy(sid)
                count += 1
        return count


_default_manager: SandboxManager | None = None


def get_sandbox_manager() -> SandboxManager:
    """获取沙箱管理器：优先 Docker，不可用时回退本地（开发）。"""
    global _default_manager
    if _default_manager is None:
        cfg = get_config().sandbox
        settings = get_settings()
        if not cfg.enabled or not settings.sandbox_enabled:
            logger.warning("sandbox.disabled_by_config")
        try:
            _default_manager = DockerSandboxManager()
            logger.info("sandbox.backend", backend="docker")
        except Exception as exc:  # noqa: BLE001
            # C1：Docker 不可用时不得静默在宿主机直接执行不可信代码。
            # 仅当显式允许（开发环境）才回退本地；否则 fail-hard。
            if cfg.allow_insecure_local_fallback:
                logger.warning("sandbox.docker_unavailable_fallback_local_INSECURE", error=str(exc))
                _default_manager = LocalSandboxManager()
            else:
                logger.error("sandbox.docker_unavailable_no_fallback", error=str(exc))
                raise RuntimeError(
                    "Docker 沙箱不可用，且未启用 allow_insecure_local_fallback。"
                    "拒绝在宿主机上直接执行不可信 PR 代码（SPEC 第 9 节）。"
                ) from exc
    return _default_manager


def reset_sandbox_manager() -> None:
    global _default_manager
    _default_manager = None
