# PR Review Agent with Sandbox Execution

**简体中文** | [English](README.en.md)

一个具备沙箱执行能力的 **PR 智能审查 Agent**。系统自动读取 GitHub Pull Request，分析代码变更，在隔离环境中运行测试、lint 与安全扫描，识别可验证的问题，并在条件允许时生成经过沙箱验证的修复建议。

> 核心定位：自动化审查助手，帮助开发者更早发现高置信度缺陷、安全风险、回归问题和可维护性问题。输出的是**证据化审查结果**，而非模型私有思考链。

## 核心特性

- **变更理解** — 读取 PR 元数据、diff、变更文件与项目上下文，理解变更影响范围
- **静态审查** — 检查逻辑错误、安全问题、边界条件、风格与可维护性问题
- **动态验证** — 在沙箱中运行测试、lint、SAST 与最小复现脚本，将发现转化为可验证证据
- **保守修复** — 对高置信度、低风险、小范围问题生成 patch 并沙箱验证后发布
- **证据链输出** — 每条发现附带代码位置、diff、触发条件、复现方式、工具输出与验证结果
- **安全隔离** — 所有不可信代码执行在网络隔离、资源受限、短生命周期沙箱中完成

## 架构

```
GitHub Webhook → POST /api/v1/webhooks/github → Orchestrator
                                      ├─ Planner（审查计划）
                                      ├─ Security Reviewer（安全审查）
                                      ├─ Bug Hunter（缺陷审查）
                                      ├─ Test Executor（沙箱测试执行）
                                      ├─ Issue Aggregator（发现聚合）
                                      ├─ Patch Generator（修复生成与验证）
                                      └─ Review Publisher（审查发布）
                                           ↓
                                    Tool Gateway（统一审计/限流）
                                      ├─ GitHub API Tool
                                      ├─ Sandbox Manager（Docker 隔离）
                                      ├─ Analyzer Tool（lint/SAST）
                                      └─ Memory Tool（可选长期记忆）
```

### 审查状态机

```
RECEIVED → VALIDATING_EVENT → PREPROCESSING → PLANNING
  → SANDBOX_PREPARE → STATIC_REVIEW → DYNAMIC_TESTING
  → ISSUE_AGGREGATION → PATCH_GENERATION → PATCH_VALIDATION
  → REVIEW_PUBLISHING → COMPLETED

异常：FAILED | CANCELLED | SUPERSEDED | TIMEOUT | SKIPPED
```

**幂等规则**：每个任务由 `repository_id + pr_number + head_sha` 唯一标识。重复 webhook 返回已有任务；新 commit 到达时旧任务进入 `SUPERSEDED` 并销毁沙箱。

### 审查流水线阶段

1. **预处理** — 校验 webhook 签名，拉取 PR 元数据、changed files、unified diff 与文件内容
2. **规划** — 识别语言/风险区域，判断变更类型（文档/测试/代码/配置/依赖），生成任务图与沙箱需求；纯文档变更直接 `SKIPPED`
3. **静态审查** — Security Reviewer（启发式规则 + 沙箱内 SAST + LLM）与 Bug Hunter 并行分析
4. **动态验证** — 在隔离沙箱中运行测试/lint/type check，区分环境/依赖/代码失败
5. **聚合** — 去重、按严重度与置信度排序、抑制低置信度无佐证发现、判定 patch 资格
6. **补丁生成与验证** — 仅对高置信度小范围问题生成最小修复，沙箱验证通过（`passed`）才作为 GitHub suggestion 发布
7. **发布** — 行级 review comment / suggestion / summary，绑定 head SHA、去重、安全高危精简披露

### 设计原则

- **工具约束优先** — Agent 不直接触碰 GitHub/文件系统/网络/执行环境，一切经 Tool Gateway（统一审计/限流/校验）
- **证据优先于推测** — 每条发现附代码位置、类型、严重度、置信度、证据与是否已验证；低置信度无佐证不伪装为确定缺陷
- **最小权限与凭据隔离** — PR 代码视为不可信输入，Token/云凭据/内网绝不进入沙箱
- **保守自动修复** — 仅高置信度、小范围、可验证、不改外部行为的问题才生成可提交 patch
- **幂等与可取消** — 同键幂等、新 commit 取代旧任务、超时退出、沙箱失败安全回收

## 快速开始

### 环境要求

- Python ≥ 3.10
- Docker（沙箱执行，可选；开发环境可回退本地执行）
- Redis（可选；未配置时回退进程内内存）
- PostgreSQL（可选；开发环境默认 SQLite）

### 安装

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### 配置

```bash
cp .env.example .env
# 编辑 .env 填入 GitHub App 凭据或 PAT、LLM API Key 等
cp config/config.example.yaml config/config.yaml
# 按需覆盖默认配置
```

关键配置项见 `config/default.yaml`。

### 构建沙箱镜像（可选，用于动态测试）

```bash
./docker/sandbox/build.sh
```

### 启动服务

```bash
pr-review-agent serve              # 使用 .env 配置
pr-review-agent serve --port 9000  # 指定端口
```

### 手动触发审查

```bash
pr-review-agent review --repo org/repo --pr 42 --sha abc123
```

### 查看任务状态

```bash
pr-review-agent status --review-id repo-123-pr-42-sha-abc123def456
```

## GitHub App 配置

推荐最小权限：

| 权限 | 级别 | 用途 |
|------|------|------|
| Pull requests | Read & write | 读取 PR、发布 review |
| Contents | Read | 读取代码 |
| Checks | Read & write | 发布 check result |
| Metadata | Read | 基础 repo metadata |
| Issues | Read & write | 发布 PR comment |

设置 Webhook URL 为 `https://your-host/api/v1/webhooks/github`，Content-Type 为 `application/json`，Secret 填入 `GITHUB_WEBHOOK_SECRET`。

## REST API

完整的 REST API contract（路径、请求/响应 schema、鉴权方式、状态码）见 [`docs/openapi.yaml`](docs/openapi.yaml)（OpenAPI 3.0），也可在服务启动后访问 `/docs` 查看交互式文档。

| Method | Path | 鉴权 | 说明 |
|---|---|---|---|
| GET | `/health` | 无 | 健康检查，供负载均衡器/编排系统探活 |
| POST | `/api/v1/webhooks/github` | HMAC 签名（`X-Hub-Signature-256`） | 接收 GitHub webhook，创建审查任务 |
| GET | `/api/v1/reviews` | `Authorization: Bearer <API_KEY>` | 分页列出最近的审查任务 |
| GET | `/api/v1/reviews/{review_id}` | `Authorization: Bearer <API_KEY>` | 查询单个审查任务详情 |

`/api/v1/reviews*` 需要在 `.env` 中配置 `API_KEY`；未配置时按 fail-closed 策略拒绝请求（`503`），而非放行不设防的管理接口。

## 沙箱安全

沙箱遵循最小权限与强隔离设计：

- **两阶段网络模型**：依赖安装阶段受限网络（allowlist registry），测试执行阶段完全无网络
- **容器安全**：非 root、禁止 privileged、rootfs 只读、no-new-privileges、cgroup 资源限制
- **凭据隔离**：GitHub Token / 云凭据 / 数据库凭据永不进入沙箱；代码归档由管理器（容器外）下载后注入
- **生命周期**：默认 ≤30 分钟，自动回收

| 风险等级 | 隔离方式 |
|---------|---------|
| 低风险内部仓库 | Docker + seccomp + AppArmor + cgroup |
| 中风险私有仓库 | gVisor 或 Kata Containers |
| 高风险公开仓库 | Firecracker microVM |

## 项目结构

```
PRReviewAgent/
├── app/
│   ├── main.py              # FastAPI 入口
│   ├── cli.py               # 命令行工具
│   ├── config.py            # 配置加载（YAML + 环境变量）
│   ├── models/              # Pydantic 数据模型（ReviewTask/Issue/Patch/Plan/Blackboard）
│   ├── db/                  # SQLAlchemy ORM + 仓储层
│   ├── orchestrator/        # 编排器 + 状态机
│   ├── agents/              # 各专业 Agent
│   ├── tools/               # 工具网关 + GitHub/Sandbox/Analyzer/Memory 工具
│   ├── blackboard/          # 子 Agent 共享状态
│   ├── llm/                 # LLM 客户端 + 提示词
│   ├── api/                 # REST API：/health、/api/v1/webhooks、/api/v1/reviews
│   └── logging_setup.py     # 结构化日志（脱敏）
├── config/                  # YAML 配置
├── docker/sandbox/          # 沙箱镜像 Dockerfile
├── docs/openapi.yaml        # REST API contract（OpenAPI 3.0）
├── tests/                   # 测试套件
└── pyproject.toml
```

## 测试

```bash
pytest tests/ -v
```

测试覆盖：数据模型、状态机、分析器、聚合器、配置、GitHub 工具、LLM 客户端、Memory 工具、REST API（health/webhooks/reviews）。

## 降级策略

系统支持部分成功：

- 静态审查成功、动态测试失败 → 仍发布静态发现
- 测试成功、patch 失败 → 发布 issue，不发布 patch
- SAST 失败 → 继续运行 lint 和测试
- memory 不可用 → 不阻断审查
- GitHub suggestion 不可用 → 降级为 patch comment

## License

MIT
