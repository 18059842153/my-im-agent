"""企业微信服务端 API：access_token 缓存 + 应用主动发消息。

主动发消息正是「定时闹钟」的基础——企业微信自建应用允许服务端主动推送，
不需要等待用户先说话。
"""

from __future__ import annotations

import asyncio
import time

import httpx

QYAPI_BASE = "https://qyapi.weixin.qq.com"

# access_token 失效相关错误码：触发一次刷新重试
_TOKEN_EXPIRED_CODES = {40014, 42001}


class WeComAPIError(Exception):
    """企业微信 API 返回错误。"""

    def __init__(self, errcode: int, errmsg: str) -> None:
        super().__init__(f"wecom api error {errcode}: {errmsg}")
        self.errcode = errcode
        self.errmsg = errmsg


class WeComAPI:
    def __init__(self, corp_id: str, secret: str, agent_id: int) -> None:
        self._corp_id = corp_id
        self._secret = secret
        self._agent_id = agent_id
        self._client = httpx.AsyncClient(base_url=QYAPI_BASE, timeout=15.0)
        self._token: str | None = None
        self._token_expire_at: float = 0.0
        self._token_lock = asyncio.Lock()

    async def access_token(self, *, force_refresh: bool = False) -> str:
        async with self._token_lock:
            if not force_refresh and self._token and time.time() < self._token_expire_at - 60:
                return self._token
            resp = await self._client.get(
                "/cgi-bin/gettoken",
                params={"corpid": self._corp_id, "corpsecret": self._secret},
            )
            data = resp.json()
            if data.get("errcode", 0) != 0:
                raise WeComAPIError(data.get("errcode", -1), data.get("errmsg", "unknown"))
            self._token = data["access_token"]
            self._token_expire_at = time.time() + int(data.get("expires_in", 7200))
            return self._token

    async def send_text(self, user_id: str, content: str, *, _retried: bool = False) -> None:
        """给指定成员发文本消息。"""
        await self._send_message(
            {"touser": user_id, "msgtype": "text", "text": {"content": content}},
            _retried=_retried,
        )

    async def send_markdown(self, user_id: str, content: str, *, _retried: bool = False) -> None:
        """给指定成员发 markdown 消息（支持加粗与链接，单条约 4096 字节）。"""
        await self._send_message(
            {"touser": user_id, "msgtype": "markdown", "markdown": {"content": content}},
            _retried=_retried,
        )

    async def _send_message(self, payload: dict, *, _retried: bool = False) -> None:
        token = await self.access_token()
        body = {"agentid": self._agent_id, "safe": 0, **payload}
        resp = await self._client.post(
            "/cgi-bin/message/send",
            params={"access_token": token},
            json=body,
        )
        data = resp.json()
        errcode = data.get("errcode", 0)
        if errcode == 0:
            return
        if errcode in _TOKEN_EXPIRED_CODES and not _retried:
            await self.access_token(force_refresh=True)
            return await self._send_message(payload, _retried=True)
        raise WeComAPIError(errcode, data.get("errmsg", "unknown"))

    async def aclose(self) -> None:
        await self._client.aclose()
