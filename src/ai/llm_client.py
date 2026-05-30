"""OpenAI 兼容协议的轻量 LLM 客户端。

- 只依赖 requests，不强依赖 openai SDK，base_url 任意替换。
- 失败有重试 + 退避；多次失败会让 .usable 变 False，调用方应回退。
"""
from __future__ import annotations

import json
from typing import Any

import requests
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from src.utils.config_loader import load_env
from src.utils.logger import get_logger

logger = get_logger()


class LLMUnavailable(RuntimeError):
    pass


class LLMClient:
    """OpenAI Chat Completions 兼容客户端。"""

    def __init__(self, role: str = "default"):
        env = load_env()
        self.api_key = env["OPENAI_API_KEY"]
        self.base_url = env["OPENAI_BASE_URL"].rstrip("/")
        self.model = env["KEYWORD_MODEL"] if role == "keyword" else env["OPENAI_MODEL"]
        self.temperature = env["AI_TEMPERATURE"]
        self.max_tokens = env["AI_MAX_TOKENS"]
        self.max_retries = env["AI_MAX_RETRIES"]
        self.enabled = env["ENABLE_AI"]
        self._fail_count = 0

    @property
    def usable(self) -> bool:
        return bool(self.enabled and self.api_key and self.base_url and self.model)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(min=1, max=8),
        retry=retry_if_exception_type((requests.RequestException, LLMUnavailable)),
        reraise=True,
    )
    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        r = requests.post(url, headers=headers, data=json.dumps(payload), timeout=60)
        if r.status_code >= 500:
            raise LLMUnavailable(f"server {r.status_code}: {r.text[:200]}")
        if r.status_code == 429:
            raise LLMUnavailable("rate limited")
        r.raise_for_status()
        return r.json()

    def chat(self, prompt: str, system: str | None = None) -> str:
        if not self.usable:
            raise LLMUnavailable("LLM 未启用或缺少配置")
        if self._fail_count >= max(self.max_retries, 1) * 3:
            raise LLMUnavailable("连续失败次数过多，已熔断")

        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        try:
            data = self._post(payload)
        except Exception as e:
            self._fail_count += 1
            logger.warning(f"LLM 调用失败：{e}")
            raise

        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise LLMUnavailable(f"LLM 返回结构异常：{e} body={str(data)[:200]}")
        self._fail_count = 0
        return content or ""
