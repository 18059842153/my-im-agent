"""每日新闻归档：写 `<date>.md` / `<date>.html` 与 `latest.*`，并生成下载地址。

归档文件由 `news_server` 以静态文件形式暴露，企业微信里只推一个链接，
完整版（全部条目 + 全部要点）留给手机浏览器慢慢看。
"""

from __future__ import annotations

import html
import os
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote

if TYPE_CHECKING:  # 避免与 news.py 形成运行时循环导入
    from my_im_agent.news import NewsItem, NewsSummary, Section

DigestSections = Sequence["tuple[Section, list[tuple[NewsItem, NewsSummary]]]"]


@dataclass(frozen=True)
class ArchiveResult:
    day: str
    markdown_path: Path
    html_path: Path
    url: str = ""


def _when(item: NewsItem) -> str:
    return f" · {item.published:%m-%d %H:%M}" if item.published else ""


def render_markdown(*, now: datetime, digests: DigestSections) -> str:
    total = sum(len(pairs) for _, pairs in digests)
    lines = [
        f"# 每日新闻 · {now:%Y-%m-%d}",
        "",
        f"> 生成时间 {now:%H:%M} · 共 {total} 条 · "
        + " / ".join(f"{section.label} {len(pairs)} 条" for section, pairs in digests if pairs),
        "",
    ]
    for section, pairs in digests:
        if not pairs:
            continue
        lines.extend([f"## {section.label}", ""])
        for index, (item, summary) in enumerate(pairs, 1):
            title = summary.title or item.title
            if item.link:
                lines.append(f"### {index}. [{title}]({item.link})")
            else:
                lines.append(f"### {index}. {title}")
            lines.append("")
            lines.append(f"来源：{item.source}{_when(item)}")
            for point in summary.points:
                lines.append(f"- {point}")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


_CSS = """
*{box-sizing:border-box}
body{margin:0;padding:18px 16px 40px;background:#f5f6f8;color:#1f2329;
  font:16px/1.65 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif}
main{max-width:760px;margin:0 auto}
h1{font-size:22px;margin:0 0 6px}
.meta{color:#8a9099;font-size:13px;margin-bottom:22px}
h2{font-size:18px;margin:28px 0 10px;padding-left:10px;border-left:4px solid #3370ff}
article{background:#fff;border-radius:12px;padding:14px 16px;margin-bottom:12px;
  box-shadow:0 1px 3px rgba(0,0,0,.06)}
h3{font-size:16px;margin:0 0 6px;line-height:1.45}
a{color:#1f2329;text-decoration:none}
.src{color:#8a9099;font-size:12px;margin-bottom:8px}
ul{margin:0;padding-left:20px}
li{margin:4px 0}
footer{color:#a0a6ad;font-size:12px;text-align:center;margin-top:26px}
"""


def render_html(*, now: datetime, digests: DigestSections) -> str:
    total = sum(len(pairs) for _, pairs in digests)
    parts = [
        "<!doctype html>",
        '<html lang="zh-CN"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        f"<title>每日新闻 · {now:%Y-%m-%d}</title>",
        f"<style>{_CSS}</style></head><body><main>",
        f"<h1>每日新闻 · {now:%Y-%m-%d}</h1>",
        f'<div class="meta">生成时间 {now:%H:%M} · 共 {total} 条</div>',
    ]
    for section, pairs in digests:
        if not pairs:
            continue
        parts.append(f"<h2>{html.escape(section.label)}</h2>")
        for index, (item, summary) in enumerate(pairs, 1):
            title = html.escape(summary.title or item.title)
            if item.link:
                heading = f'<a href="{html.escape(item.link, quote=True)}">{title}</a>'
            else:
                heading = title
            points = "".join(f"<li>{html.escape(point)}</li>" for point in summary.points)
            parts.append(
                "<article>"
                f"<h3>{index}. {heading}</h3>"
                f'<div class="src">{html.escape(item.source)}{_when(item)}</div>'
                f"<ul>{points}</ul>"
                "</article>"
            )
    parts.append(
        f'<footer>my-im-agent · {now:%Y-%m-%d %H:%M}</footer></main></body></html>'
    )
    return "\n".join(parts)


def _write_atomic(path: Path, content: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


class NewsArchive:
    """当天 md + html 归档，同时刷新 latest.* 供手机随时打开。"""

    def __init__(
        self,
        directory: Path | str,
        *,
        public_base_url: str = "",
        token: str = "",
    ) -> None:
        self._directory = Path(directory)
        self._public_base_url = public_base_url.rstrip("/")
        self._token = token

    @property
    def directory(self) -> Path:
        return self._directory

    def url_for(self, day: str, *, suffix: str = "html") -> str:
        if not self._public_base_url:
            return ""
        url = f"{self._public_base_url}/news/{day}.{suffix}"
        if self._token:
            url += f"?token={quote(self._token)}"
        return url

    def save(self, *, now: datetime, digests: DigestSections, day: str | None = None) -> ArchiveResult:
        day = day or f"{now:%Y-%m-%d}"
        markdown = render_markdown(now=now, digests=digests)
        page = render_html(now=now, digests=digests)

        self._directory.mkdir(parents=True, exist_ok=True)
        markdown_path = self._directory / f"{day}.md"
        html_path = self._directory / f"{day}.html"
        _write_atomic(markdown_path, markdown)
        _write_atomic(html_path, page)
        _write_atomic(self._directory / "latest.md", markdown)
        _write_atomic(self._directory / "latest.html", page)
        return ArchiveResult(
            day=day,
            markdown_path=markdown_path,
            html_path=html_path,
            url=self.url_for(day),
        )
