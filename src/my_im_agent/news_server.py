"""新闻归档的只读文件服务：`/news/<date>.html|md`。

安全约束：
- 只允许 `YYYY-MM-DD.(md|html)` 与 `latest.(md|html)`，杜绝目录穿越
- 配置了 IM_AGENT_NEWS_TOKEN 时，必须带 `?token=...`
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, RedirectResponse

_NAME_RE = re.compile(r"^(?:\d{4}-\d{2}-\d{2}|latest)\.(?:md|html)$")
_MEDIA = {
    "md": "text/markdown; charset=utf-8",
    "html": "text/html; charset=utf-8",
}


def create_app(*, archive_dir: Path | str, token: str = "") -> FastAPI:
    root = Path(archive_dir).resolve()
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    def _authorize(value: str | None) -> None:
        if token and value != token:
            raise HTTPException(status_code=403, detail="invalid token")

    @app.get("/news")
    @app.get("/news/")
    def index() -> RedirectResponse:
        # 不能把 token 拼进跳转地址，否则任何人访问 /news/ 都能拿到口令
        return RedirectResponse("/news/latest.html")

    @app.get("/news/{name}")
    def read_news(name: str, token_q: str | None = Query(None, alias="token")) -> FileResponse:
        _authorize(token_q)
        if not _NAME_RE.match(name):
            raise HTTPException(status_code=404, detail="not found")
        path = (root / name).resolve()
        if path.parent != root or not path.is_file():
            raise HTTPException(status_code=404, detail="not found")
        suffix = name.rsplit(".", 1)[-1]
        return FileResponse(path, media_type=_MEDIA[suffix])

    return app


async def serve(app: FastAPI, *, host: str = "0.0.0.0", port: int = 8677) -> None:
    import uvicorn

    server = uvicorn.Server(
        uvicorn.Config(app, host=host, port=port, log_level="warning", access_log=False)
    )
    await server.serve()
