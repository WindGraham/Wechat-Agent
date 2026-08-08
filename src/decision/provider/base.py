# -*- coding: utf-8 -*-
"""provider/base.py — LLM 供应商接口定义。"""

import base64
import logging
import time

import requests

log = logging.getLogger("decision.provider")


class ProviderError(RuntimeError):
    """LLM 调用失败（异常信息只带 HTTP 状态码/异常类型，不带 key）。"""


class LLMProvider:
    """OpenAI 兼容 provider 基类。

    子类只需提供 url/model/是否省略 temperature。k3 是思考型模型，
    只允许 temperature=1（传参即 400），omit_temperature=True 时不带该参数。
    """

    def __init__(self, api_key: str, model: str, base_url: str,
                 omit_temperature: bool = False, timeout: int = 60):
        if not api_key:
            raise ProviderError("API key 为空")
        self._key = api_key
        self.model = model
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._omit_temperature = omit_temperature
        self._timeout = timeout
        self._sess = requests.Session()
        self._sess.headers.update({
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        })

    # ---------------------------------------------------------------- 文本
    def chat(self, messages, max_tokens=300, temperature=0.8) -> str:
        """POST chat/completions。5xx/429 退避重试 1 次，4xx 直接抛。"""
        payload = {"model": self.model, "messages": messages,
                   "max_tokens": max_tokens}
        if not self._omit_temperature:
            payload["temperature"] = temperature
        last_err = None
        for attempt in range(2):
            try:
                r = self._sess.post(self._url, json=payload,
                                    timeout=self._timeout)
            except requests.RequestException as e:
                last_err = type(e).__name__
                log.warning("llm request failed (attempt %d): %s",
                            attempt, last_err)
                if attempt == 0:
                    time.sleep(2)
                    continue
                raise ProviderError(f"LLM request failed: {last_err}")
            if r.status_code == 200:
                return r.json()["choices"][0]["message"]["content"].strip()
            if r.status_code == 429 or r.status_code >= 500:
                log.warning("llm HTTP %d (attempt %d)", r.status_code, attempt)
                if attempt == 0:
                    time.sleep(4)
                    continue
                raise ProviderError(f"LLM API error: HTTP {r.status_code} (retried)")
            raise ProviderError(f"LLM API error: HTTP {r.status_code}")
        raise ProviderError("unreachable")

    # ---------------------------------------------------------------- 视觉
    def vision(self, image_bytes: bytes, question: str,
               max_tokens: int = 500) -> str:
        """多模态读图（base64 image_url，OpenAI 格式）。默认实现走 chat 接口。"""
        b64 = base64.b64encode(image_bytes).decode("ascii")
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": question},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/jpeg;base64,{b64}",
                               "detail": "auto"}},
            ],
        }]
        return self.chat(messages, max_tokens=max_tokens)

    def vision_file(self, path: str, question: str,
                    max_tokens: int = 500) -> str:
        with open(path, "rb") as f:
            return self.vision(f.read(), question, max_tokens=max_tokens)


class KimiProvider(LLMProvider):
    """Kimi for Coding API（k3 等）。k3 只允许 temperature=1 → 省略该参数。"""

    def __init__(self, api_key: str, model: str = "k3", **kw):
        super().__init__(api_key, model,
                         "https://api.kimi.com/coding/v1",
                         omit_temperature=True, **kw)


class DeepSeekProvider(LLMProvider):
    """DeepSeek API（备用）。"""

    def __init__(self, api_key: str, model: str = "deepseek-chat", **kw):
        super().__init__(api_key, model,
                         "https://api.deepseek.com", **kw)
