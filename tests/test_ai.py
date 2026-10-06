# -*- coding: utf-8 -*-
"""AI 解读层测试。

核心契约：**AI 是可选的**。不配置任何服务商时，全部本地能力照常工作，
只是 AI 解读不可用 —— 这条必须被测试守住，否则很容易在某次重构里
悄悄变成硬依赖。

本文件不发起任何真实网络请求：用 monkeypatch 替换 httpx。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from log_ai_compressor import ai, service as S
from log_ai_compressor.ai import client as ai_client
from log_ai_compressor.ai.config import AIConfig, PROVIDERS, load_config

BASE = Path(__file__).resolve().parent.parent
SAMPLE = BASE / "examples" / "sample_system.log"


@pytest.fixture(autouse=True)
def _isolate_config(tmp_path, monkeypatch):
    """每个用例都用独立的配置文件 + 清空环境变量，互不污染。"""
    from log_ai_compressor.ai import config as cfg_mod
    monkeypatch.setattr(cfg_mod, "CONFIG_FILE", tmp_path / "ai.json")
    for meta in PROVIDERS.values():
        if meta.get("env_key"):
            monkeypatch.delenv(meta["env_key"], raising=False)
    monkeypatch.setenv("LOG_AI_PROVIDER", "none")
    yield


@pytest.fixture(scope="module")
def result():
    return S.analyze(paths=[str(SAMPLE)], params={"levels": ["ERROR", "FAIL"]})


# ---------------------------------------------------------------------------
# 可选性（最重要的一条）
# ---------------------------------------------------------------------------
class TestOptional:
    def test_default_disabled(self):
        cfg = load_config()
        assert cfg.enabled is False
        assert cfg.provider == "none"

    def test_describe_reports_reason(self):
        st = ai.describe_config()
        assert st["available"] is False
        assert "不受影响" in st["reason"]

    def test_local_analysis_works_without_key(self, result):
        """没有 Key 时聚类 / 根因 / 导出必须完全正常。"""
        assert result.clusters
        assert S.result_to_dict(result)["stats"]["total_lines"] > 0
        assert len(S.export_text(result, "md")) > 200

    def test_explain_raises_clean_error(self, result):
        with pytest.raises(ai.LLMError, match="未启用"):
            ai.explain_result(result)

    def test_explain_cluster_raises_clean_error(self, result):
        with pytest.raises(ai.LLMError, match="未启用"):
            ai.explain_cluster(result, result.clusters[0].cluster_id)


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
class TestConfig:
    def test_builtin_providers_present(self):
        st = ai.describe_config()
        keys = {p["key"] for p in st["providers"]}
        assert {"none", "deepseek", "qwen", "glm", "kimi", "openai",
                "ollama", "custom"} <= keys

    def test_cloud_provider_defaults(self):
        cfg = load_config({"provider": "deepseek", "api_key": "k"})
        assert cfg.base_url == "https://api.deepseek.com/v1"
        assert cfg.model == "deepseek-chat"
        assert cfg.enabled is True

    def test_ollama_needs_no_key(self):
        cfg = load_config({"provider": "ollama"})
        assert cfg.enabled is True, "本地 Ollama 不该要求 API Key"
        assert cfg.native_ollama is True

    def test_cloud_provider_without_key_disabled(self):
        cfg = load_config({"provider": "deepseek"})
        assert cfg.enabled is False, "没有 Key 的云端服务商不该算已启用"

    def test_env_key_picked_up(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "from-env")
        assert load_config({"provider": "deepseek"}).api_key == "from-env"

    def test_explicit_key_beats_env(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "from-env")
        assert load_config({"provider": "deepseek",
                            "api_key": "explicit"}).api_key == "explicit"

    def test_unknown_provider_falls_back(self):
        cfg = load_config({"provider": "no-such-provider"})
        assert cfg.enabled is False

    def test_public_view_never_leaks_key(self):
        cfg = AIConfig(provider="deepseek", base_url="u", model="m",
                       api_key="sk-super-secret-value")
        public = json.dumps(cfg.to_public())
        assert "sk-super-secret-value" not in public
        assert '"api_key"' not in public
        assert public.count("*") >= 6, "应给出打码后的 Key 便于用户确认"

    def test_short_key_fully_masked(self):
        cfg = AIConfig(provider="custom", base_url="u", model="m", api_key="abc")
        assert cfg.masked_key() == "***"

    def test_save_and_reload(self, tmp_path):
        from log_ai_compressor.ai.config import save_config
        save_config(AIConfig(provider="glm", base_url="https://x/v4",
                             model="glm-4-flash", api_key="k1"))
        again = load_config()
        assert again.provider == "glm" and again.api_key == "k1"

    def test_corrupt_config_file_does_not_crash(self, monkeypatch, tmp_path):
        """配置文件损坏只应退回默认，不该让整个工具起不来。"""
        from log_ai_compressor.ai import config as cfg_mod
        bad = tmp_path / "ai.json"
        bad.write_text("{ this is not json", encoding="utf-8")
        monkeypatch.setattr(cfg_mod, "CONFIG_FILE", bad)
        assert load_config().provider == "none"


# ---------------------------------------------------------------------------
# 提示词
# ---------------------------------------------------------------------------
class TestPrompts:
    def test_explain_prompt_contains_evidence(self, result):
        p = ai.build_explain_prompt(S.result_to_dict(result, top_n=3,
                                                     with_samples=False))
        assert "日志概况" in p
        assert "错误簇明细" in p
        assert "先查哪里" in p
        assert any(c.summary in p for c in result.clusters[:3])

    def test_explain_prompt_is_compact(self, result):
        """压缩的意义就在这里：提示词必须远小于原始日志。"""
        raw_size = len(SAMPLE.read_text(encoding="utf-8"))
        p = ai.build_explain_prompt(S.result_to_dict(result, top_n=5,
                                                     with_samples=False))
        assert len(p) < raw_size

    def test_explain_prompt_carries_extra_question(self, result):
        p = ai.build_explain_prompt(S.result_to_dict(result, with_samples=False),
                                    question="为什么 Redis 报错？")
        assert "为什么 Redis 报错" in p

    def test_explain_prompt_marks_missing_timestamps(self, result):
        payload = S.result_to_dict(result, with_samples=False)
        payload["stats"]["time_range_text"] = ""
        payload["global_hist"] = {"series": []}
        p = ai.build_explain_prompt(payload)
        assert "无时间戳" in p and "无时间分布" in p

    def test_explain_prompt_includes_time_shape(self, result):
        """回归：直方图结构改过（属性 vs 方法），prompt 侧要能正确读出形态。"""
        payload = S.result_to_dict(result, with_samples=False)
        assert payload["global_hist"]["series"], "样例应带时间直方图"
        p = ai.build_explain_prompt(payload)
        shape = [ln for ln in p.splitlines() if "错误时间形态" in ln][0]
        assert "峰值" in shape and "形态" in shape

    def test_cluster_prompt_includes_stack(self):
        text = ("2024-01-01 10:00:00 ERROR [db] connect failed\n"
                "java.net.ConnectException: refused\n"
                "\tat com.app.db.Pool.init(Pool.java:42)\n"
                "\tat java.base/java.lang.Thread.run(Thread.java:840)\n")
        r = S.analyze(text=text)
        ctx = {"stats": S.result_to_dict(r, with_samples=False)["stats"],
               "cluster": S.cluster_to_dict(r.clusters[0], full=True),
               "cooccurring": []}
        p = ai.build_cluster_prompt(ctx)
        assert "降噪堆栈" in p
        assert "已折叠" in p
        assert "业务帧" in p
        assert "怎么修" in p

    def test_system_prompt_forbids_hallucination(self):
        assert "不要编造" in ai.SYSTEM_ANALYST
        assert "证据不足" in ai.SYSTEM_ANALYST

    def test_prompts_escape_nothing_but_are_text(self, result):
        """提示词是纯文本，不含可执行标记（避免日志内容注入指令）。"""
        p = ai.build_explain_prompt(S.result_to_dict(result, with_samples=False))
        assert isinstance(p, str)
        assert "<script" not in p.lower()


# ---------------------------------------------------------------------------
# 客户端（mock 网络）
# ---------------------------------------------------------------------------
@pytest.fixture
def fake_post(monkeypatch):
    """替换 httpx.Client.post，记录请求体并返回可控响应。"""
    calls = []

    class FakeResp:
        def __init__(self, payload, status=200):
            self._payload = payload
            self.status_code = status
            self.text = json.dumps(payload, ensure_ascii=False)

        def json(self):
            return self._payload

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, headers=None, json=None):
            calls.append({"url": url, "headers": headers, "body": json})
            return FakeResp(calls[-1].get("response", {}))

    monkeypatch.setattr(ai_client.httpx, "Client", FakeClient)
    return calls


class TestClient:
    def _cfg(self, **kw):
        base = dict(provider="deepseek",
                    base_url="https://api.deepseek.com/v1",
                    model="deepseek-chat", api_key="k")
        base.update(kw)
        return AIConfig(**base)

    def test_openai_compatible_request(self, fake_post):
        fake_post[0] if False else None
        calls = fake_post

        def _post(url, headers=None, json=None):
            calls.append({"url": url, "headers": headers, "body": json})
            return type("R", (), {
                "status_code": 200, "text": "{}",
                "json": lambda self: {"choices": [{"message": {"content": "好"}}]},
            })()

        import httpx
        orig = httpx.Client
        httpx.Client = type("C", (), {
            "__init__": lambda s, *a, **k: None,
            "__enter__": lambda s: s, "__exit__": lambda s, *a: False,
            "post": staticmethod(_post),
        })
        try:
            out = ai_client.chat("测试", cfg=self._cfg())
        finally:
            httpx.Client = orig
        assert out == "好"
        assert calls[0]["url"] == "https://api.deepseek.com/v1/chat/completions"
        assert calls[0]["headers"]["Authorization"] == "Bearer k"
        assert calls[0]["body"]["model"] == "deepseek-chat"
        assert calls[0]["body"]["messages"][0]["role"] == "system"

    def test_ollama_native_endpoint(self, fake_post):
        calls = fake_post

        def _post(url, headers=None, json=None):
            calls.append({"url": url, "headers": headers, "body": json})
            return type("R", (), {
                "status_code": 200, "text": "{}",
                "json": lambda self: {"message": {"content": "本地回答"}},
            })()

        import httpx
        orig = httpx.Client
        httpx.Client = type("C", (), {
            "__init__": lambda s, *a, **k: None,
            "__enter__": lambda s: s, "__exit__": lambda s, *a: False,
            "post": staticmethod(_post),
        })
        try:
            out = ai_client.chat("测试", cfg=self._cfg(
                provider="ollama", base_url="http://127.0.0.1:11434",
                model="qwen2.5:7b", api_key="", native_ollama=True))
        finally:
            httpx.Client = orig
        assert out == "本地回答"
        assert calls[0]["url"] == "http://127.0.0.1:11434/api/chat"
        assert "Authorization" not in calls[0]["headers"]

    def test_disabled_config_rejected_before_network(self, fake_post):
        with pytest.raises(ai.LLMError, match="未启用"):
            ai_client.chat("x", cfg=AIConfig(provider="none"))
        assert not fake_post, "未启用时不该发出任何请求"

    @pytest.mark.parametrize("status,expect", [
        (401, "API Key"), (404, "模型名"), (429, "限流"),
    ])
    def test_http_error_messages(self, status, expect, fake_post):
        import httpx
        orig = httpx.Client

        def _post(url, headers=None, json=None):
            return type("R", (), {
                "status_code": status, "text": "err",
                "json": lambda self: {"error": {"message": "boom"}},
            })()

        httpx.Client = type("C", (), {
            "__init__": lambda s, *a, **k: None,
            "__enter__": lambda s: s, "__exit__": lambda s, *a: False,
            "post": staticmethod(_post),
        })
        try:
            with pytest.raises(ai.LLMError) as exc:
                ai_client.chat("x", cfg=self._cfg())
        finally:
            httpx.Client = orig
        assert expect in str(exc.value)

    def test_empty_response_rejected(self, fake_post):
        import httpx
        orig = httpx.Client
        httpx.Client = type("C", (), {
            "__init__": lambda s, *a, **k: None,
            "__enter__": lambda s: s, "__exit__": lambda s, *a: False,
            "post": staticmethod(lambda url, headers=None, json=None: type(
                "R", (), {"status_code": 200, "text": "{}",
                          "json": lambda self: {"choices": []}})()),
        })
        try:
            with pytest.raises(ai.LLMError, match="空内容"):
                ai_client.chat("x", cfg=self._cfg())
        finally:
            httpx.Client = orig

    def test_connection_error_gives_ollama_hint(self, fake_post):
        import httpx
        orig = httpx.Client

        def _post(url, headers=None, json=None):
            raise httpx.ConnectError("refused")

        httpx.Client = type("C", (), {
            "__init__": lambda s, *a, **k: None,
            "__enter__": lambda s: s, "__exit__": lambda s, *a: False,
            "post": staticmethod(_post),
        })
        try:
            with pytest.raises(ai.LLMError, match="ollama serve"):
                ai_client.chat("x", cfg=self._cfg(
                    provider="ollama", base_url="http://127.0.0.1:11434",
                    model="m", api_key="", native_ollama=True))
        finally:
            httpx.Client = orig


# ---------------------------------------------------------------------------
# 端到端（mock 网络下跑通 explain）
# ---------------------------------------------------------------------------
class TestExplainEndToEnd:
    def test_explain_result_with_mock(self, result, monkeypatch):
        monkeypatch.setattr(ai, "chat",
                             lambda prompt, cfg=None, **kw: "这是解读")
        assert ai.explain_result(result, cfg=AIConfig(
            provider="deepseek", base_url="u", model="m", api_key="k")) == "这是解读"

    def test_explain_cluster_with_mock(self, result, monkeypatch):
        monkeypatch.setattr(ai, "chat", lambda prompt, cfg=None, **kw: "簇解读")
        out = ai.explain_cluster(result, result.clusters[0].cluster_id,
                                 cfg=AIConfig(provider="deepseek", base_url="u",
                                              model="m", api_key="k"))
        assert out == "簇解读"

    def test_explain_cluster_unknown_id(self, result):
        cfg = AIConfig(provider="deepseek", base_url="u", model="m", api_key="k")
        with pytest.raises(ai.LLMError, match="不存在"):
            ai.explain_cluster(result, 99999, cfg=cfg)
