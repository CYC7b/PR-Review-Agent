"""导出 FastAPI 应用的 OpenAPI schema 到 docs/openapi.yaml。

docs/openapi.yaml 是 REST API 的权威 contract；路由/schema 变更后需重新运行本脚本
同步，而不是手工编辑 YAML。

用法：
    python scripts/export_openapi.py
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PATH = PROJECT_ROOT / "docs" / "openapi.yaml"

_NULL_SCHEMA = {"type": "null"}


def _downgrade_to_openapi_30(node: object) -> None:
    """把 pydantic v2 / JSON Schema 2020-12 风格的产物转成 OpenAPI 3.0 兼容形式。

    FastAPI 的 openapi_version 参数只改 `openapi:` 头部字符串，不会转换 schema
    本身，需要手工降级，否则文档对严格的 3.0 校验器/代码生成器无效：
      1. `anyOf: [X, {type: null}]` → `nullable: true`（3.0 无 null 类型）
      2. `examples: [...]`（数组） → `example: ...`（单值，3.0 只支持单值）
    """
    if isinstance(node, list):
        for item in node:
            _downgrade_to_openapi_30(item)
        return
    if not isinstance(node, dict):
        return

    any_of = node.get("anyOf")
    if isinstance(any_of, list) and any(item == _NULL_SCHEMA for item in any_of):
        remaining = [item for item in any_of if item != _NULL_SCHEMA]
        del node["anyOf"]
        node["nullable"] = True
        if len(remaining) == 1:
            only = remaining[0]
            if "$ref" in only:
                node["allOf"] = [only]
            else:
                node.update(only)
        elif remaining:
            node["anyOf"] = remaining

    if isinstance(node.get("examples"), list) and node["examples"]:
        node["example"] = node.pop("examples")[0]

    for value in node.values():
        _downgrade_to_openapi_30(value)


def main() -> None:
    from app.main import create_app

    app = create_app()
    app.openapi_version = "3.0.3"
    schema = app.openapi()
    _downgrade_to_openapi_30(schema)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_PATH.open("w", encoding="utf-8") as fh:
        # 经 json round-trip 归一化（例如 Enum 值），再以稳定顺序写出 YAML。
        yaml.safe_dump(
            json.loads(json.dumps(schema)),
            fh,
            sort_keys=False,
            allow_unicode=True,
        )
    print(f"OpenAPI schema written to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
