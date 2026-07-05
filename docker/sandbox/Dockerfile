# PR Review Agent —— 沙箱执行环境镜像（SPEC 第 9 节）
#
# 安全约束：
#   - 非 root 用户运行（reviewer）
#   - 禁止 privileged mode（由运行时 docker run 保证）
#   - 禁止挂载 docker socket / hostPath（由运行时保证）
#   - no-new-privileges（由运行时 security_opt 保证）
#   - 网络隔离（默认 network=none，依赖阶段受限网络）
#
# 包含语言运行时与审查工具：
#   - Python 3.11 + ruff/mypy/pytest/bandit/semgrep
#   - Node.js 20 + eslint
#   - Go + golangci-lint
#   - git（用于 diff/patch）

FROM python:3.11-slim AS base

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# 系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates gnupg git git-lfs \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Node.js 20
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Go
ENV GOLANG_VERSION=1.22.2 \
    GOROOT=/usr/local/go \
    GOPATH=/go \
    PATH="/usr/local/go/bin:/go/bin:${PATH}"
RUN curl -fsSL "https://go.dev/dl/go${GOLANG_VERSION}.linux-amd64.tar.gz" \
    | tar -C /usr/local -xz

# Python 审查工具
RUN pip install --no-cache-dir \
    ruff==0.4.4 \
    mypy==1.10.0 \
    pytest==8.2.0 \
    pytest-asyncio==0.23.7 \
    bandit==1.7.9 \
    semgrep==1.77.0

# Node 审查工具
RUN npm install -g eslint@8.57.0 typescript@5.4.5

# Go 审查工具
RUN go install github.com/golangci/golangci-lint/cmd/golangci-lint@v1.59.1

# 创建非 root 用户（SPEC 9.2）
RUN groupadd -r reviewer && useradd -r -g reviewer -m -d /home/reviewer -s /bin/bash reviewer

# 工作目录（运行时挂载为 tmpfs）
RUN mkdir -p /workspace && chown reviewer:reviewer /workspace
WORKDIR /workspace

USER reviewer

# 默认保持容器存活（由 sandbox.create 以 sleep infinity 启动）
CMD ["sleep", "infinity"]
