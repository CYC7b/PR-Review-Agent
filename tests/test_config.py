"""配置系统测试（SPEC 第 13 节）。"""

from __future__ import annotations

from app.config import get_config, get_settings


class TestConfig:
    def test_default_config(self):
        cfg = get_config()
        assert cfg.review.max_changed_files == 100
        assert cfg.review.skip_docs_only is True
        assert cfg.review.enable_auto_patch is False
        assert cfg.sandbox.enabled is True
        assert cfg.patch.max_retry == 2
        assert cfg.patch.min_confidence_for_patch == 0.8
        assert cfg.memory.enable_repo_memory is False
        assert cfg.github.bind_to_head_sha is True

    def test_trigger_events(self):
        cfg = get_config()
        assert "pull_request.opened" in cfg.review.trigger_events
        assert "pull_request.synchronize" in cfg.review.trigger_events

    def test_dependency_allowlist(self):
        cfg = get_config()
        assert "pypi.org" in cfg.sandbox.dependency_network_allowlist
        assert "registry.npmjs.org" in cfg.sandbox.dependency_network_allowlist

    def test_env_settings(self):
        settings = get_settings()
        assert settings.llm_mode == "mock"  # 由 conftest 设置
        assert settings.database_url.startswith("sqlite")

    def test_config_merge(self, tmp_path, monkeypatch):
        """测试 YAML 配置合并。"""
        import yaml

        from app import config as config_module
        from app.config import reload_config

        # 写入用户配置文件，模拟 config.yaml
        user_cfg = {"review": {"max_changed_files": 50, "enable_auto_patch": True},
                    "patch": {"max_retry": 5}}
        user_yaml = tmp_path / "config.yaml"
        user_yaml.write_text(yaml.dump(user_cfg), encoding="utf-8")

        # 让 _load_yaml 在加载 "config.yaml" 时使用临时文件
        original_load_yaml = config_module._load_yaml

        def patched_load_yaml(path):
            if path.name == "config.yaml":
                return user_cfg
            return original_load_yaml(path)

        monkeypatch.setattr(config_module, "_load_yaml", patched_load_yaml)
        cfg = reload_config()[0]
        assert cfg.review.max_changed_files == 50
        assert cfg.review.enable_auto_patch is True
        assert cfg.patch.max_retry == 5
        # 未覆盖的保持默认
        assert cfg.review.skip_docs_only is True
