"""确保 docs/openapi.yaml 与 FastAPI app 的实际 schema 保持同步。

路由/schema 变更后必须重新运行 scripts/export_openapi.py；本测试防止有人忘记这一步
导致提交的 contract 文件与实现悄悄漂移。
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from scripts.export_openapi import _downgrade_to_openapi_30

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONTRACT_PATH = PROJECT_ROOT / "docs" / "openapi.yaml"


def test_openapi_contract_matches_app_schema():
    from app.main import create_app

    app = create_app()
    app.openapi_version = "3.0.3"
    schema = app.openapi()
    _downgrade_to_openapi_30(schema)
    generated = json.loads(json.dumps(schema))

    with CONTRACT_PATH.open("r", encoding="utf-8") as fh:
        committed = yaml.safe_load(fh)

    assert generated == committed, (
        "docs/openapi.yaml 与当前实现不一致，请运行 "
        "`python scripts/export_openapi.py` 后重新提交。"
    )
