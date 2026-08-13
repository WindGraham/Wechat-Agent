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

    token 上下限（2026-08-11，网关可热调）：
    - token_floor：max_tokens 下限（思考型模型的 reasoning 会吃额度，
      给太小 content 为空；k3/deepseek-v4 默认 256）
    - token_ceiling：max_tokens 上限（0 = 不封顶）
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
        self._token_floor = 0          # 0 = 无下限（子类可设 256）
        self._token_ceiling = 0        # 0 = 不封顶
        self._last_usage = {}          # 最近一次响应的 usage（缓存统计用）
        self._sess = requests.Session()
        self._sess.headers.update({
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        })

    def cache_stats(self) -> dict:
        """归一化 usage → 缓存命中统计。

        k3/DeepSeek: usage.cached_tokens；Gemini: usageMetadata.
        cachedContentTokenCount（中转站当前不返回 → 0）。
        返回 {cached_tokens, prompt_tokens, total}。"""
        u = self._last_usage or {}
        cached = (u.get("cached_tokens") or
                  u.get("cachedContentTokenCount") or 0)
        prompt = (u.get("prompt_tokens") or
                  u.get("promptTokenCount") or 0)
        return {"cached_tokens": int(cached),
                "prompt_tokens": int(prompt),
                "total": int(cached) + int(prompt)}

    def set_token_limits(self, floor: int = 0, ceiling: int = 0):
        """设置 max_tokens 下限/上限（网关热调用）。

        传 0 表示该项**保留当前值不覆盖**——provider 自带的下限
        （思考型模型 256）不会被启动时的空配置擦掉。想恢复下限默认值，
        显式传 provider 的 MIN_MAX_TOKENS。"""
        if floor:
            self._token_floor = max(0, int(floor))
        if ceiling:
            self._token_ceiling = max(0, int(ceiling))

    def _clamp_tokens(self, max_tokens: int) -> int:
        if self._token_floor:
            max_tokens = max(max_tokens, self._token_floor)
        if self._token_ceiling:
            max_tokens = min(max_tokens, self._token_ceiling)
        return max_tokens

    # ---------------------------------------------------------------- 文本
    def chat(self, messages, max_tokens=8192, temperature=0.8) -> str:
        """POST chat/completions。5xx/429 退避重试 1 次，4xx 直接抛。"""
        payload = {"model": self.model, "messages": messages,
                   "max_tokens": self._clamp_tokens(max_tokens)}
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
                data = r.json()
                self._last_usage = data.get("usage", {}) or {}
                return data["choices"][0]["message"]["content"].strip()
            if r.status_code == 429 or r.status_code >= 500:
                log.warning("llm HTTP %d (attempt %d)", r.status_code, attempt)
                if attempt == 0:
                    time.sleep(4)
                    continue
                raise ProviderError(f"LLM API error: HTTP {r.status_code} (retried)")
            raise ProviderError(f"LLM API error: HTTP {r.status_code}")
        raise ProviderError("unreachable")

    # ---------------------------------------------------------------- 带思考的文本
    def chat_full(self, messages, max_tokens=8192, temperature=0.8):
        """chat + reasoning（thinking）一并返回。

        返回 (content: str, thinking: str)。thinking 为空字符串表示
        模型未返回思考（非思考型模型/未开启）。内容抽取兼容 OpenAI
        与 Kimi 两种格式（reasoning_content / reasoning）。
        """
        payload = {"model": self.model, "messages": messages,
                   "max_tokens": self._clamp_tokens(max_tokens)}
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
                data = r.json()
                self._last_usage = data.get("usage", {}) or {}
                msg = data["choices"][0]["message"]
                content = (msg.get("content") or "").strip()
                thinking = ""
                for key in ("reasoning_content", "reasoning"):
                    val = msg.get(key)
                    if isinstance(val, str):
                        thinking = val.strip()
                        break
                return content, thinking
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
    """Kimi for Coding API（k3 等）。k3 只允许 temperature=1 → 省略该参数。

    注意：k3 是思考型模型，reasoning 会消耗 max_tokens——上限给太小
    （<100）会出现 content 为空（finish_reason=length）。默认下限 256。"""

    MIN_MAX_TOKENS = 256

    def __init__(self, api_key: str, model: str = "k3", **kw):
        super().__init__(api_key, model,
                         "https://api.kimi.com/coding/v1",
                         omit_temperature=True, **kw)
        self._token_floor = self.MIN_MAX_TOKENS

    def chat(self, messages, max_tokens=8192, temperature=0.8) -> str:
        # 不设小上限（2026-08-13 用户定）：思考型模型 reasoning 吃额度，
        # 给小了 content 会空/截断。交给基类默认 8192，配合 token_floor 兜底。
        return super().chat(messages, max_tokens=max_tokens,
                            temperature=temperature)


class DeepSeekProvider(LLMProvider):
    """DeepSeek API（备用）。base_url https://api.deepseek.com。

    注意：v4 系列（deepseek-v4-pro/flash，1M 上下文）是 always_thinking
    思考型模型，reasoning 会消耗 max_tokens——与 k3 同一个坑。
    max_tokens 不设小上限（走基类默认 8192），仅保 token_floor 下限防空。"""

    MIN_MAX_TOKENS = 256

    def __init__(self, api_key: str, model: str = "deepseek-chat", **kw):
        super().__init__(api_key, model,
                         "https://api.deepseek.com", **kw)
        self._token_floor = self.MIN_MAX_TOKENS

    def chat(self, messages, max_tokens=8192, temperature=0.8) -> str:
        return super().chat(messages, max_tokens=max_tokens,
                            temperature=temperature)
