"""每日新闻推送：抓 RSS → 补正文 → 去重选条 → AI 压缩要点 → 渲染 → 定时推送。

owner 划分（与 alarms.py 同风格）：
- host：抓取、解析、补正文、去重、选条、渲染、切分、归档、幂等记账、定时、发送
- model：只负责把「标题 + 正文摘录」压成 2~3 条事实要点（Agently ModelRequest），
  失败一律走正文/标题兜底

设计要点见 docs/SPEC.md「新闻推送」章：
- 每天 09:30 触发，90 分钟补发窗口，超出窗口记为 skipped（不推过期早报）
- at-most-once：先落盘 sending 再发送，宁可漏发也不重复推两条长早报
- 科技 15 条 / 综合 15 条 / AI 前沿 10 条，各自一条消息，超长按行切分
- 当天完整版同时归档为 md + html，可通过 IM_AGENT_NEWS_PUBLIC_BASE_URL 下载
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import threading
import xml.etree.ElementTree as ET
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, time, timedelta
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

import httpx
from pydantic import BaseModel, Field

from my_im_agent.alarms import NotifyFn
from my_im_agent.config import Settings
from my_im_agent.news_archive import NewsArchive

logger = logging.getLogger(__name__)

DEFAULT_CATCHUP = timedelta(minutes=90)
SEEN_RETENTION_DAYS = 7
MAX_ATTEMPTS = 3
_ATOM_NS = "{http://www.w3.org/2005/Atom}"
# 掘金没有官方 RSS，默认走公共 RSSHub；自建时用 IM_AGENT_NEWS_RSSHUB_BASE 指向内网实例
RSSHUB_BASE = os.environ.get("IM_AGENT_NEWS_RSSHUB_BASE", "https://rsshub.app").rstrip("/")


# --------------------------------------------------------------------------- #
# 值对象
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class NewsItem:
    id: str
    title: str
    link: str
    source: str
    published: datetime | None = None
    detail: str = ""  # RSS 摘要或抓到的正文摘录（纯文本）


@dataclass(frozen=True)
class FeedSource:
    name: str
    url: str


@dataclass(frozen=True)
class Section:
    key: str
    label: str
    feeds: tuple[FeedSource, ...]
    limit: int | None = None  # None 表示用 IM_AGENT_NEWS_TOP_N


@dataclass
class SectionResult:
    section: Section
    items: list[NewsItem] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


SECTIONS: tuple[Section, ...] = (
    Section(
        "tech",
        "科技",
        (
            FeedSource("IT之家", "https://www.ithome.com/rss/"),
            FeedSource("少数派", "https://sspai.com/feed"),
        ),
    ),
    Section(
        "general",
        "综合",
        (FeedSource("中国新闻网", "https://www.chinanews.com.cn/rss/scroll-news.xml"),),
    ),
    Section(
        "ai",
        "AI 前沿",
        (
            # 掘金没有官方 RSS，走 RSSHub 的 AI 分类；自建 RSSHub 时替换域名即可
            FeedSource("掘金 AI", f"{RSSHUB_BASE}/juejin/category/ai"),
            FeedSource("机器之心", "https://www.jiqizhixin.com/rss"),
            FeedSource("量子位", "https://www.qbitai.com/feed"),
            FeedSource("InfoQ 中文", "https://www.infoq.cn/feed"),
        ),
        limit=10,
    ),
)


def make_item_id(title: str, link: str) -> str:
    raw = (link or title).strip()
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


# --------------------------------------------------------------------------- #
# 解析层（纯函数，无 IO）
# --------------------------------------------------------------------------- #


class FeedParseError(ValueError):
    """内容不是可解析的 RSS/Atom。"""


_XML_DECL = re.compile(r"^\s*<\?xml[^>]*\?>")
_BARE_AMP = re.compile(
    r"&(?!(?:amp|lt|gt|quot|apos|#\d+|#x[0-9A-Fa-f]+);)"
)
_CDATA = re.compile(r"(<!\[CDATA\[.*?\]\]>)", re.S)


def _sanitize_entities(text: str) -> str:
    """转义 CDATA 之外的裸 & / 未定义实体；CDATA 内容必须原样保留。"""
    return "".join(
        part if part.startswith("<![CDATA[") else _BARE_AMP.sub("&amp;", part)
        for part in _CDATA.split(text)
    )


_DECL_ENC = re.compile(rb"""^\s*<\?xml[^>]*encoding=["']([A-Za-z0-9._-]+)["']""")
_TAG_RE = re.compile(r"<[^>]+>")


def decode_xml(raw: bytes) -> str:
    """嗅探声明里的编码解码，并剥掉 XML 声明。

    xml.etree 遇到 `encoding="gb2312"` 会直接抛
    `ValueError: multi-byte encodings are not supported`（传 bytes 也一样），
    所以必须自己解码；而保留 `<?xml ...?>` 声明去 fromstring(str) 又会报
    `Unicode strings with encoding declaration are not supported`，故一并剥掉。
    """
    match = _DECL_ENC.match(raw[:200])
    declared = match.group(1).decode("ascii").lower() if match else "utf-8"
    if declared in {"gb2312", "gbk", "gb18030"}:
        declared = "gb18030"  # 超集，兜住越界脏字节
    try:
        text = raw.decode(declared, errors="replace")
    except LookupError:
        text = raw.decode("utf-8", errors="replace")
    text = _XML_DECL.sub("", text, count=1)
    # 部分源（如机器之心）会输出裸 & 或 &nbsp; 这类 XML 未定义实体，先转义再解析
    return _sanitize_entities(text)


def parse_published(raw: str | None) -> datetime | None:
    """解析 pubDate / updated，统一成本地 naive 时间。

    `CST` 是国内源常见写法，但 email 库按美国中部时间（-06:00）解析，
    会把"今天 10:00"算成 14 小时前，导致排序错乱。这里特殊处理。
    """
    if not raw or not raw.strip():
        return None
    text = raw.strip()

    # CST / PRC / HKT 一律按 +08:00 处理
    if re.search(r"\b(CST|PRC|HKT)\b", text):
        text = re.sub(r"\b(CST|PRC|HKT)\b", "+0800", text)

    parsed: datetime | None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError):
            return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(microsecond=0)
    return parsed.astimezone().replace(tzinfo=None, microsecond=0)


def clean_title(raw: str | None, *, source: str) -> str:
    if not raw:
        return ""
    text = _TAG_RE.sub("", raw)  # 去掉内联 HTML
    text = " ".join(text.replace("\u3000", " ").split())
    for suffix in (f" - {source}", f" | {source}", f"_ {source}"):
        if text.endswith(suffix):
            text = text[: -len(suffix)].strip()
    return text


class _TextExtractor(HTMLParser):
    """把 HTML 压成保留段落边界的纯文本；脚本/样式直接跳过。"""

    _BLOCK = {
        "p", "br", "div", "li", "tr", "section", "article", "blockquote",
        "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "table",
    }
    _SKIP = {"script", "style", "noscript", "svg", "iframe", "nav", "footer"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag in self._BLOCK:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in self._BLOCK:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self._parts.append(data)

    def text(self) -> str:
        lines: list[str] = []
        for raw in "".join(self._parts).splitlines():
            line = " ".join(raw.replace("\u3000", " ").split())
            if line:
                lines.append(line)
        return "\n".join(lines)


_ARTICLE_RE = re.compile(r"<article\b[^>]*>(.*?)</article>", re.S | re.I)
_WECHAT_RE = re.compile(
    r"""<div\b[^>]*\bid=["']js_content["'][^>]*>(.*?)</div>\s*</div>""", re.S | re.I
)
_MAIN_RE = re.compile(r"<main\b[^>]*>(.*?)</main>", re.S | re.I)


_NAV_HINTS = (
    "首页", "IT圈", "最会买", "日夜间", "随系统", "主题色", "无障碍",
    "扫码登录", "扫码关注", "登录 注册", "客户端下载", "关注公众号",
    "责任编辑", "免责声明", "版权所有", "相关阅读", "上一篇", "下一篇",
    "即时 时政", "投稿", "订阅", "RSS", "软媒", "App客户端", "要知App",
    "浅色", "深色", "收藏IT之家", "软媒魔方",
)


_NAV_TOKENS = frozenset(
    {
        "设置", "浅色", "深色", "黑色", "首页", "IT圈", "最会买", "登录",
        "注册", "搜索", "投稿", "订阅", "RSS订阅", "收藏IT之家", "软媒应用",
        "App客户端", "要知App", "软媒魔方", "业界", "手机", "电脑", "测评",
        "视频", "AI", "苹果", "iPhone", "鸿蒙", "软件", "智车", "数码",
        "学院", "游戏", "直播", "5G",
    }
)
_DATE_RE = re.compile(
    r"^\d{4}[-/.]\d{1,2}[-/.]\d{1,2}(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?$"
    r"|^\d{1,2}:\d{2}(?::\d{2})?$"
)


def _strip_boilerplate(text: str, *, title: str = "", source: str = "") -> str:
    """去掉正文里混入的站点头部/导航/推荐位，避免模型把栏目名当新闻事实。"""
    full_title = re.sub(r"[\s\W_]+", "", title.lower())
    norm_title = full_title[:12]
    out: list[str] = []
    for raw in text.splitlines():
        line = " ".join(raw.split())
        if not line:
            continue
        norm = re.sub(r"[\s\W_]+", "", line.lower())
        if line in _NAV_TOKENS or _DATE_RE.match(line):
            continue
        if full_title and norm.startswith(full_title) and len(norm) <= len(full_title) + 6:
            continue
        if norm_title and norm == norm_title:
            continue
        if line.startswith(("作者：", "责编：", "编辑：", "评论：")):
            continue
        if re.fullmatch(r"【[^】]{0,20}】", line):
            continue
        if ("公众号" in line or "发自" in line) and len(line) < 40:
            continue
        # 正文开头常见「标题 / 作者 / 时间 / 栏目条」页头，逐行剥掉
        if not out:
            if line.count("|") >= 2:
                continue
            if len(line) < 25 and not re.search(r"[。！？!?，,]", line):
                continue
        if "src=" in line or "< img" in line.lower() or "<img" in line.lower():
            continue
        hits = sum(1 for hint in _NAV_HINTS if hint in line)
        if norm_title and norm.startswith(norm_title):
            # 标题后面拖着作者/来源/导航（常见于 IT之家/量子位/中新网），整行丢弃
            if hits or "来源：" in line or (source and source in line):
                continue
        # 多个栏目名拼成一行、且没有句子标点 → 导航条
        if hits >= 2 and "。" not in line and "，" not in line:
            continue
        if hits and len(line) < 100:
            continue
        # 「标题 作者 时间 来源：xxx」这类页头
        if "来源：" in line and len(line) < 120:
            continue
        out.append(line)
    return "\n".join(out)


def html_to_text(html: str, *, limit: int | None = None) -> str:
    """HTML → 纯文本（纯函数）。优先取 article/main 正文，退化为整页。"""
    if not html:
        return ""
    for pattern in (_ARTICLE_RE, _MAIN_RE, _WECHAT_RE):
        match = pattern.search(html)
        if match:
            html = match.group(1)
            break
    extractor = _TextExtractor()
    try:
        extractor.feed(html)
        extractor.close()
    except Exception:  # noqa: BLE001 - 脏 HTML 不能让整轮抓取失败
        return ""
    text = extractor.text()
    if limit is not None and len(text) > limit:
        text = text[:limit].rstrip()
    return text


def _feed_detail(raw: str | None, *, limit: int = 600) -> str:
    """RSS/Atom 描述 → 纯文本摘录。"""
    if not raw or not raw.strip():
        return ""
    return html_to_text(raw, limit=limit)


def _text(node: ET.Element | None) -> str:
    return (node.text or "").strip() if node is not None else ""


def _rss_link(item: ET.Element) -> str:
    link = _text(item.find("link"))
    if link:
        return link
    guid = item.find("guid")
    if guid is not None and _text(guid).startswith("http"):
        return _text(guid)
    return ""


def _atom_link(entry: ET.Element) -> str:
    fallback = ""
    for link in entry.findall(f"{_ATOM_NS}link"):
        href = (link.get("href") or "").strip()
        if not href:
            continue
        rel = (link.get("rel") or "alternate").strip()
        if rel == "alternate":
            return href
        if not fallback:
            fallback = href
    return fallback


def _make_item(
    *,
    title: str,
    link: str,
    source: str,
    published_raw: str | None,
    detail: str = "",
) -> NewsItem | None:
    title = clean_title(title, source=source)
    if not title:
        return None
    if link and not link.startswith(("http://", "https://")):
        link = ""
    return NewsItem(
        id=make_item_id(title, link),
        title=title,
        link=link,
        source=source,
        published=parse_published(published_raw),
        detail=detail,
    )


def parse_feed(raw: bytes, *, source: str) -> list[NewsItem]:
    """解析 RSS 2.0 或 Atom。解析不出条目时抛 FeedParseError。"""
    text = decode_xml(raw)
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise FeedParseError(f"{source}: XML 解析失败：{exc}") from exc

    if root.tag.endswith("rss") or root.tag == "rss":
        items = _parse_rss(root, source)
    elif root.tag == f"{_ATOM_NS}feed":
        items = _parse_atom(root, source)
    else:
        raise FeedParseError(f"{source}: 不是 RSS/Atom（根节点 {root.tag}）")

    if not items:
        raise FeedParseError(f"{source}: 没有可解析的条目")
    return items


def _parse_rss(root: ET.Element, source: str) -> list[NewsItem]:
    content_ns = "{http://purl.org/rss/1.0/modules/content/}"
    out: list[NewsItem] = []
    for node in root.iter("item"):
        detail_raw = _text(node.find("description")) or _text(node.find(f"{content_ns}encoded"))
        item = _make_item(
            title=_text(node.find("title")),
            link=_rss_link(node),
            source=source,
            published_raw=_text(node.find("pubDate")) or _text(node.find("date")),
            detail=_feed_detail(detail_raw),
        )
        if item is not None:
            out.append(item)
    return out


def _parse_atom(root: ET.Element, source: str) -> list[NewsItem]:
    out: list[NewsItem] = []
    for entry in root.findall(f"{_ATOM_NS}entry"):
        title = _text(entry.find(f"{_ATOM_NS}title"))
        published = _text(entry.find(f"{_ATOM_NS}published")) or _text(entry.find(f"{_ATOM_NS}updated"))
        detail_raw = _text(entry.find(f"{_ATOM_NS}summary")) or _text(
            entry.find(f"{_ATOM_NS}content")
        )
        item = _make_item(
            title=title,
            link=_atom_link(entry),
            source=source,
            published_raw=published,
            detail=_feed_detail(detail_raw),
        )
        if item is not None:
            out.append(item)
    return out


# --------------------------------------------------------------------------- #
# 抓取层
# --------------------------------------------------------------------------- #


class NewsFetcher:
    """并发抓多个 RSS；单源失败只记 errors，绝不抛给上层。"""

    def __init__(
        self,
        *,
        timeout: float = 15.0,
        max_bytes: int = 3_000_000,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._timeout = timeout
        self._max_bytes = max_bytes
        self._transport = transport
        self._client = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            transport=transport,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (compatible; my-im-agent/0.3; +https://github.com/18059842153/my-im-agent)"
                )
            },
        )

    async def fetch(self, section: Section) -> SectionResult:
        result = SectionResult(section=section)
        gathered = await asyncio.gather(
            *(self._fetch_one(source) for source in section.feeds),
            return_exceptions=True,
        )
        for source, outcome in zip(section.feeds, gathered, strict=False):
            if isinstance(outcome, BaseException):
                result.errors.append(f"{source.name}: {outcome}")
                logger.warning("fetch %s failed: %s", source.name, outcome)
                continue
            result.items.extend(outcome)

        result.items.sort(key=lambda i: i.published or datetime.min, reverse=True)
        return result

    async def _fetch_one(self, source: FeedSource) -> list[NewsItem]:
        response = await self._client.get(source.url)
        if response.status_code != 200:
            raise RuntimeError(f"HTTP {response.status_code}")
        raw = response.content[: self._max_bytes]
        return parse_feed(raw, source=source.name)

    async def aclose(self) -> None:
        await self._client.aclose()


class ArticleFetcher:
    """按条目链接补抓正文；单篇失败保留 RSS 描述，绝不拖垮整轮。"""

    def __init__(
        self,
        *,
        timeout: float = 12.0,
        max_bytes: int = 400_000,
        concurrency: int = 5,
        chars: int = 1200,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._max_bytes = max(1, max_bytes)
        self._chars = max(200, chars)
        self._sem = asyncio.Semaphore(max(1, concurrency))
        self._client = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            transport=transport,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (compatible; my-im-agent/0.3; +https://github.com/18059842153/my-im-agent)"
                )
            },
        )

    async def enrich(self, items: Sequence[NewsItem]) -> list[NewsItem]:
        outcomes = await asyncio.gather(
            *(self._fetch_one(item) for item in items), return_exceptions=True
        )
        enriched: list[NewsItem] = []
        for item, outcome in zip(items, outcomes, strict=False):
            if isinstance(outcome, BaseException):
                logger.debug("article fetch failed (%s): %s", item.link, outcome)
                enriched.append(item)
            else:
                enriched.append(outcome)
        return enriched

    async def _fetch_one(self, item: NewsItem) -> NewsItem:
        if not item.link:
            return item
        async with self._sem:
            response = await self._client.get(item.link)
        if response.status_code != 200:
            return item
        raw = response.content[: self._max_bytes]
        encoding = response.encoding or "utf-8"
        try:
            page = raw.decode(encoding, errors="replace")
        except LookupError:
            page = raw.decode("utf-8", errors="replace")
        text = _strip_boilerplate(
            html_to_text(page), title=item.title, source=item.source
        )
        if len(text) > self._chars:
            text = text[: self._chars].rstrip()
        if len(text) < 80:
            return item
        return replace(item, detail=text)

    async def aclose(self) -> None:
        await self._client.aclose()


# --------------------------------------------------------------------------- #
# 选取层（纯函数）
# --------------------------------------------------------------------------- #


def dedupe_key(item: NewsItem) -> str:
    """跨源去重用：标题归一化后取摘要（同一条新闻不同站点转载也算一条）。"""
    normalized = re.sub(r"[\s\W_]+", "", item.title.lower())
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:12]


def select_items(
    items: Sequence[NewsItem],
    *,
    limit: int,
    seen: Collection[str] = (),
    balance: bool = False,
) -> tuple[list[NewsItem], list[str]]:
    """按时间倒序去重后取前 limit 条，返回 (条目, 对应 dedupe_key)。

    balance=True 时按来源轮询取条，避免单个高产源（如 InfoQ）占满整个板块。
    """
    ordered = sorted(items, key=lambda i: i.published or datetime.min, reverse=True)
    deduped: list[tuple[NewsItem, str]] = []
    used: set[str] = set()
    for item in ordered:
        key = dedupe_key(item)
        if key in seen or key in used:
            continue
        used.add(key)
        deduped.append((item, key))

    if not balance:
        picked = deduped[:limit]
    else:
        buckets: dict[str, list[tuple[NewsItem, str]]] = {}
        for pair in deduped:
            buckets.setdefault(pair[0].source, []).append(pair)
        picked = []
        while len(picked) < limit:
            progressed = False
            for bucket in buckets.values():
                if bucket and len(picked) < limit:
                    picked.append(bucket.pop(0))
                    progressed = True
            if not progressed:
                break
    return [item for item, _ in picked], [key for _, key in picked]


# --------------------------------------------------------------------------- #
# 摘要层（模型）
# --------------------------------------------------------------------------- #


class NewsSummary(BaseModel):
    id: str = Field(description="必须原样回传 input.items[].id，不得编造")
    title: str = Field(
        min_length=1,
        max_length=120,
        description="沿用原标题；超过 24 个汉字才压缩，不得夸大",
    )
    # 注意：这里不能给默认值。给了默认值模型会把它当可选项、直接留空。
    points: list[str] = Field(
        min_length=1,
        max_length=3,
        description="2~3 条事实要点，每条 ≤45 汉字，必须保留关键实体、数字、结论或影响",
    )


class NewsDigest(BaseModel):
    items: list[NewsSummary]


class NewsSummarizer(Protocol):
    async def summarize(
        self, *, section_label: str, items: Sequence[NewsItem], now: datetime
    ) -> list[NewsSummary]: ...


def _make_summary(item_id: str, title: str, points: Sequence[str]) -> NewsSummary:
    """内部构造：绕过 min_length 校验（空要点是合法的降级态，但对模型是必填）。"""
    return NewsSummary.model_construct(id=item_id, title=title, points=list(points))


def _points_from_detail(detail: str, *, limit: int = 2) -> list[str]:
    """模型不可用时，从正文/RSS 描述里截前几句当要点。"""
    text = " ".join(detail.split())
    if not text:
        return []
    out: list[str] = []
    for part in re.split(r"[。！？!?；;]\s*", text):
        part = part.strip("，,、 ")
        if len(part) < 8:
            continue
        out.append(_clip(part, 70))
        if len(out) >= limit:
            break
    return out


def fallback_summaries(items: Sequence[NewsItem]) -> list[NewsSummary]:
    """降级：用正文/RSS 描述截句；连描述都没有就只留标题。"""
    return [
        _make_summary(i.id, i.title, _points_from_detail(i.detail)) for i in items
    ]


class AgentlyNewsSummarizer:
    """按板块分块调用模型；单块失败只让该块降级。"""

    def __init__(self, settings: Settings, *, chunk_size: int = 5) -> None:
        from my_im_agent.llm import apply_model_settings

        apply_model_settings(settings)
        self._settings = settings
        self._chunk_size = max(1, chunk_size)

    async def summarize(
        self, *, section_label: str, items: Sequence[NewsItem], now: datetime
    ) -> list[NewsSummary]:
        if not items:
            return []
        chunks = [
            list(items[i : i + self._chunk_size]) for i in range(0, len(items), self._chunk_size)
        ]
        outcomes = await asyncio.gather(
            *(self._summarize_chunk(section_label, chunk, now) for chunk in chunks),
            return_exceptions=True,
        )

        merged: list[NewsSummary] = []
        for chunk, outcome in zip(chunks, outcomes, strict=False):
            if isinstance(outcome, BaseException):
                logger.warning(
                    "summarize chunk failed (%s, %d items): %s",
                    section_label,
                    len(chunk),
                    outcome,
                )
                merged.extend(fallback_summaries(chunk))
            else:
                merged.extend(outcome)
        return merged

    async def _summarize_chunk(
        self, section_label: str, chunk: Sequence[NewsItem], now: datetime
    ) -> list[NewsSummary]:
        from agently import Agently

        request = (
            Agently.create_request()
            .input(
                {
                    "today": f"{now:%Y-%m-%d}",
                    "section": section_label,
                    "items": [
                        {
                            "id": i.id,
                            "title": i.title,
                            "source": i.source,
                            "detail": i.detail[:800],
                        }
                        for i in chunk
                    ],
                }
            )
            .instruct(
                {
                    "role": "你是中文新闻编辑，把每条新闻压缩成 30 秒能扫完的事实要点。",
                    "rules": [
                        "每条都必须输出，id 必须原样回传 input.items[].id，不得编造新 id。",
                        "title 沿用原标题；超过 24 个汉字才压缩，禁止夸大、禁止加感叹号。",
                        "points 输出 2~3 条，按重要性排序；每条一句话，不超过 45 个汉字。",
                        "每条要点必须带关键信息：主体、动作/数字、结论或影响，禁止「值得关注」这类空话。",
                        "只依据 title、source、detail 写，禁止补充记忆里的旧闻或推测细节。",
                        "detail 为空时，只写标题里明确的信息，宁可少写也不要编造。",
                        "detail 可能混入站点导航、栏目名、推荐语，必须忽略这些噪声，只保留新闻事实。",
                        "不要复述标题，不加 emoji、编号、项目符号。",
                        "输出顺序与 input.items 一致。",
                    ],
                }
            )
            .output(NewsDigest, format="json")
        )
        digest = await request.async_get_data_object()
        return list(digest.items)


class EchoNewsSummarizer:
    """未配置模型 Key 时的兜底：不联网，只从 RSS 描述截要点。"""

    async def summarize(
        self, *, section_label: str, items: Sequence[NewsItem], now: datetime
    ) -> list[NewsSummary]:
        return fallback_summaries(items)


def reconcile(
    items: Sequence[NewsItem], summaries: Sequence[NewsSummary]
) -> list[tuple[NewsItem, NewsSummary]]:
    """host 侧成员校验：未知 id 丢弃、重复只留首个、缺失补位、按输入顺序重排。"""
    offered = {item.id for item in items}
    by_id: dict[str, NewsSummary] = {}
    for summary in summaries:
        if summary.id not in offered or summary.id in by_id:
            continue
        by_id[summary.id] = summary

    pairs: list[tuple[NewsItem, NewsSummary]] = []
    for item in items:
        summary = by_id.get(item.id)
        if summary is None:
            pairs.append(
                (item, _make_summary(item.id, item.title, _points_from_detail(item.detail)))
            )
            continue
        points = _tidy_points(summary.points, detail=item.detail)
        pairs.append(
            (
                item,
                _make_summary(summary.id, summary.title or item.title, points),
            )
        )
    return pairs


def _tidy_points(points: Sequence[str], *, detail: str = "", limit: int = 3) -> list[str]:
    """归一化要点：单行、去项目符号、去重、限长；全空时退回正文截句。"""
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in points:
        text = " ".join(str(raw).split()).lstrip("·-•*　 ").strip()
        if not text:
            continue
        text = _clip(text, 70)
        key = re.sub(r"[\s\W_]+", "", text.lower())
        if not key or key in seen:
            continue
        seen.add(key)
        cleaned.append(text)
        if len(cleaned) >= limit:
            break
    return cleaned or _points_from_detail(detail)


# --------------------------------------------------------------------------- #
# 渲染层（纯函数）
# --------------------------------------------------------------------------- #


_MD_UNSAFE = re.compile(r"[*_\[\]()#>\x60]")
_HARD_SPLIT_SAFE = "，。、；：！？,.;:!?"


def md_safe(text: str) -> str:
    """企业微信 markdown 不支持反斜杠转义，所以只能删掉会破坏语法的字符。"""
    return _MD_UNSAFE.sub("", " ".join(str(text).split())).strip()


def safe_url(url: str) -> str:
    return quote(url, safe="/:?&=#%+@!$'~-_.*,;")


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip("，,、 ") + "…"


def render_section(
    *,
    label: str,
    now: datetime,
    pairs: Sequence[tuple[NewsItem, NewsSummary]],
) -> str:
    """渲染一个板块。链接内联在标题上，省掉一行「阅读原文」，能显著压低字节数。"""
    lines = [f"**📰 {label} · {now:%m月%d日 %H:%M}**", ""]
    for index, (item, summary) in enumerate(pairs, 1):
        title = _clip(md_safe(summary.title) or md_safe(item.title) or "（无标题）", 46)
        if item.link:
            lines.append(f"{index}. **[{title}]({safe_url(item.link)})**")
        else:
            lines.append(f"{index}. **{title}**")
        for point in summary.points[:2]:  # 推送只放前 2 条，完整要点在归档里
            text = _clip(md_safe(point), 70)
            if text:
                lines.append(f"   · {text}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def split_markdown(text: str, *, limit: int = 3800) -> list[str]:
    """按行累积切分（中文按 UTF-8 字节算），绝不切断单条新闻。

    切分时预留 24 字节给后续追加的「（续 n/m）」页头，避免加上页头后超限。
    """

    def size(value: str) -> int:
        return len(value.encode("utf-8"))

    budget = max(120, limit - 24)
    chunks: list[list[str]] = [[]]
    current = 0
    for line in text.split("\n"):
        line_bytes = size(line) + 1
        if line_bytes > budget:
            for piece in _hard_split(line, budget):
                piece_bytes = size(piece) + 1
                if chunks[-1] and current + piece_bytes > budget:
                    chunks.append([])
                    current = 0
                chunks[-1].append(piece)
                current += piece_bytes
            continue
        if chunks[-1] and current + line_bytes > budget:
            chunks.append([])
            current = 0
        chunks[-1].append(line)
        current += line_bytes

    rendered = ["\n".join(chunk).strip("\n") for chunk in chunks]
    rendered = [chunk for chunk in rendered if chunk.strip()]
    total = len(rendered)
    return [
        chunk if index == 0 else f"（续 {index + 1}/{total}）\n{chunk}"
        for index, chunk in enumerate(rendered)
    ]


def _hard_split(line: str, limit: int) -> list[str]:
    """单行本身就超限时的兜底切分，优先在标点处断开。"""
    pieces: list[str] = []
    buffer = ""
    for char in line:
        if len((buffer + char).encode("utf-8")) > limit - 4:
            cut = max(buffer.rfind(mark) for mark in _HARD_SPLIT_SAFE)
            if cut > 0:
                pieces.append(buffer[: cut + 1])
                buffer = buffer[cut + 1 :]
            else:
                pieces.append(buffer)
                buffer = ""
        buffer += char
    if buffer:
        pieces.append(buffer)
    return pieces


# --------------------------------------------------------------------------- #
# 状态层（幂等）
# --------------------------------------------------------------------------- #


@dataclass
class NewsState:
    last_date: str | None = None
    last_at: str | None = None
    last_status: str = "none"
    last_error: str | None = None
    attempt_date: str | None = None
    attempts: int = 0
    seen: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "last_date": self.last_date,
            "last_at": self.last_at,
            "last_status": self.last_status,
            "last_error": self.last_error,
            "attempt_date": self.attempt_date,
            "attempts": self.attempts,
            "seen": self.seen,
        }

    @classmethod
    def from_dict(cls, data: dict) -> NewsState:
        return cls(
            last_date=data.get("last_date"),
            last_at=data.get("last_at"),
            last_status=str(data.get("last_status") or "none"),
            last_error=data.get("last_error"),
            attempt_date=data.get("attempt_date"),
            attempts=int(data.get("attempts") or 0),
            seen=dict(data.get("seen") or {}),
        )


def should_fire(
    state: NewsState,
    now: datetime,
    *,
    at: time,
    catchup: timedelta = DEFAULT_CATCHUP,
    max_attempts: int = MAX_ATTEMPTS,
) -> tuple[bool, str]:
    """返回 (是否该发, 原因)。原因用于日志与 --check。"""
    slot = datetime.combine(now.date(), at)
    today = now.date().isoformat()

    if state.last_date == today and state.last_status in ("ok", "partial", "sending"):
        return False, "already-sent"
    if now < slot:
        return False, "not-yet"
    if now - slot > catchup:
        return False, "expired"
    if (
        state.attempt_date == today
        and state.last_status == "failed"
        and state.attempts >= max_attempts
    ):
        return False, "gave-up"
    return True, "due"


class NewsStateStore:
    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()

    def load(self) -> NewsState:
        with self._lock:
            return self._load_unlocked()

    def _load_unlocked(self) -> NewsState:
        if not self._path.is_file():
            return NewsState()
        try:
            return NewsState.from_dict(json.loads(self._path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            logger.warning("news state %s corrupted, starting empty: %s", self._path, exc)
            return NewsState()

    def save(self, state: NewsState) -> None:
        with self._lock:
            self._save_unlocked(state)

    def _save_unlocked(self, state: NewsState) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(state.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self._path)

    def mark_attempt(self, now: datetime) -> NewsState:
        with self._lock:
            state = self._load_unlocked()
            today = now.date().isoformat()
            state.attempts = state.attempts + 1 if state.attempt_date == today else 1
            state.attempt_date = today
            state.last_date = today
            state.last_at = now.isoformat(timespec="seconds")
            state.last_status = "sending"
            state.last_error = None
            self._save_unlocked(state)
            return state

    def mark_done(
        self,
        now: datetime,
        keys: Sequence[str],
        *,
        status: str,
        error: str | None = None,
    ) -> NewsState:
        with self._lock:
            state = self._load_unlocked()
            today = now.date().isoformat()
            state.last_date = today
            state.last_at = now.isoformat(timespec="seconds")
            state.last_status = status
            state.last_error = error
            for key in keys:
                state.seen.setdefault(key, today)
            state.seen = _prune_seen(state.seen, now)
            self._save_unlocked(state)
            return state

    def mark_skipped(self, now: datetime, reason: str) -> NewsState:
        with self._lock:
            state = self._load_unlocked()
            state.last_date = now.date().isoformat()
            state.last_at = now.isoformat(timespec="seconds")
            state.last_status = "skipped"
            state.last_error = reason
            self._save_unlocked(state)
            return state


def _prune_seen(seen: dict[str, str], now: datetime, *, days: int = SEEN_RETENTION_DAYS) -> dict[str, str]:
    cutoff = (now - timedelta(days=days)).date().isoformat()
    return {key: day for key, day in seen.items() if day >= cutoff}


# --------------------------------------------------------------------------- #
# 编排层
# --------------------------------------------------------------------------- #


@dataclass
class DigestResult:
    ok: bool
    status: str
    messages: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    archive_url: str = ""


def parse_at(raw: str) -> time:
    try:
        return time.fromisoformat(raw.strip())
    except ValueError:
        logger.warning("非法 IM_AGENT_NEWS_AT=%r，回退 09:30", raw)
        return time(9, 30)


class NewsDigestService:
    def __init__(
        self,
        *,
        settings: Settings,
        state: NewsStateStore,
        fetcher: NewsFetcher,
        summarizer: NewsSummarizer,
        notify: NotifyFn,
        sections: Sequence[Section] = SECTIONS,
        article_fetcher: ArticleFetcher | None = None,
        archive: NewsArchive | None = None,
    ) -> None:
        self._settings = settings
        self._state = state
        self._fetcher = fetcher
        self._summarizer = summarizer
        self._notify = notify
        self._sections = tuple(sections)
        self._article_fetcher = article_fetcher
        self._archive = archive or NewsArchive(
            settings.news_archive_path,
            public_base_url=settings.news_public_base_url,
            token=settings.news_token,
        )
        self._at = parse_at(settings.news_at)
        self._catchup = timedelta(minutes=settings.news_catchup_minutes)

    async def deliver(
        self,
        now: datetime,
        *,
        force: bool = False,
        persist: bool = True,
        late: bool = False,
    ) -> DigestResult:
        state = self._state.load()
        if not force:
            fire, reason = should_fire(state, now, at=self._at, catchup=self._catchup)
            if not fire:
                # 只有「今天什么都没发过」才记 skipped；手动 --news-now 推送过就不要覆盖
                if reason == "expired" and persist and state.last_date != now.date().isoformat():
                    self._state.mark_skipped(now, reason)
                return DigestResult(ok=reason != "expired", status=reason, errors=[reason])

        if persist:
            state = self._state.mark_attempt(now)

        messages: list[str] = []
        keys: list[str] = []
        errors: list[str] = []
        digests: list[tuple[Section, list[tuple[NewsItem, NewsSummary]]]] = []
        try:
            async with asyncio.timeout(self._settings.news_total_timeout):
                messages, keys, errors, digests = await self._build(now, state, late=late)
        except TimeoutError:
            errors.append("整轮超时")
            logger.error("news build timed out after %ss", self._settings.news_total_timeout)
        except Exception as exc:  # noqa: BLE001 - 任何异常都要落成状态，不能让循环挂掉
            errors.append(f"构建失败：{exc}")
            logger.exception("news build failed")

        if not messages:
            if persist:
                self._state.mark_done(now, keys, status="failed", error="; ".join(errors) or "无内容")
            return DigestResult(ok=False, status="failed", errors=errors)

        archive_url = ""
        if digests:
            try:
                archived = self._archive.save(now=now, digests=digests)
                archive_url = archived.url
                if archive_url:
                    messages[-1] = (
                        messages[-1].rstrip()
                        + f"\n\n📥 完整版（全部条目）：{archive_url}"
                    )
            except OSError as exc:  # 归档失败不影响已构建的推送
                errors.append(f"归档失败：{exc}")
                logger.warning("news archive failed: %s", exc)

        sent = 0
        target = self._settings.news_target_user
        for message in messages:
            try:
                await self._notify(target, target, message)
                sent += 1
            except Exception as exc:  # noqa: BLE001
                errors.append(f"发送失败：{exc}")
                logger.exception("news send failed")

        status = "ok" if sent == len(messages) else ("partial" if sent else "failed")
        if persist:
            self._state.mark_done(now, keys, status=status, error="; ".join(errors) or None)
        return DigestResult(
            ok=sent > 0,
            status=status,
            messages=messages,
            errors=errors,
            archive_url=archive_url,
        )

    async def _build(
        self, now: datetime, state: NewsState, *, late: bool
    ) -> tuple[
        list[str], list[str], list[str], list[tuple[Section, list[tuple[NewsItem, NewsSummary]]]]
    ]:
        seen = set(state.seen)
        results = await asyncio.gather(
            *(self._build_section(section, now, seen, late=late) for section in self._sections),
            return_exceptions=True,
        )

        messages: list[str] = []
        keys: list[str] = []
        errors: list[str] = []
        digests: list[tuple[Section, list[tuple[NewsItem, NewsSummary]]]] = []
        for section, outcome in zip(self._sections, results, strict=False):
            if isinstance(outcome, BaseException):
                errors.append(f"{section.label}: {outcome}")
                logger.warning("section %s failed: %s", section.label, outcome)
                continue
            section_messages, section_keys, section_errors, pairs = outcome
            messages.extend(section_messages)
            keys.extend(section_keys)
            errors.extend(section_errors)
            if pairs:
                digests.append((section, pairs))
        return messages, keys, errors, digests

    async def _build_section(
        self, section: Section, now: datetime, seen: set[str], *, late: bool
    ) -> tuple[
        list[str], list[str], list[str], list[tuple[NewsItem, NewsSummary]]
    ]:
        fetched = await self._fetcher.fetch(section)
        errors = list(fetched.errors)
        if not fetched.items:
            return [], [], errors + [f"{section.label}: 没有抓到任何条目"], []

        picked, keys = select_items(
            fetched.items,
            limit=section.limit or self._settings.news_top_n,
            seen=seen,
            balance=section.key == "ai",
        )
        if not picked:
            return [], [], errors + [f"{section.label}: 全是已推送过的"], []

        if self._article_fetcher is not None:
            picked = await self._article_fetcher.enrich(picked)

        summaries = await self._summarizer.summarize(
            section_label=section.label, items=picked, now=now
        )
        pairs = reconcile(picked, summaries)

        header_note = "（延迟补发）" if late else ""
        rendered = render_section(label=section.label, now=now, pairs=pairs)
        if header_note:
            rendered = rendered.replace("\n\n", f"\n{header_note}\n\n", 1)
        if not summaries:
            rendered += "\n（AI 要点暂不可用，仅列正文摘录）"

        chunks = split_markdown(rendered, limit=max(500, self._settings.news_max_bytes))
        return chunks, keys, errors, pairs


# --------------------------------------------------------------------------- #
# 调度层
# --------------------------------------------------------------------------- #


class NewsScheduler:
    """常驻循环：每分钟检查一次是否到点。与 AlarmScheduler 同构（start 不返回）。"""

    def __init__(
        self,
        service: NewsDigestService,
        settings: Settings,
        *,
        interval: float = 60.0,
    ) -> None:
        self._service = service
        self._settings = settings
        self._interval = interval
        self._stopped = asyncio.Event()

    async def start(self) -> None:
        self._stopped.clear()
        while not self._stopped.is_set():
            try:
                await self._tick(datetime.now())
            except Exception:  # noqa: BLE001 - 一次失败不能让常驻循环退出
                logger.exception("news tick failed")
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=self._interval)
            except TimeoutError:
                pass

    def stop(self) -> None:
        self._stopped.set()

    async def _tick(self, now: datetime) -> DigestResult:
        if not self._settings.news_enabled:
            return DigestResult(ok=True, status="disabled")
        slot = datetime.combine(now.date(), self._service._at)  # noqa: SLF001 - 同模块协作
        late = now > slot + timedelta(minutes=1)
        result = await self._service.deliver(now, late=late)
        if result.messages:
            logger.info("news digest sent: status=%s messages=%d", result.status, len(result.messages))
        elif result.status in ("failed",):
            logger.warning("news digest failed: %s", "; ".join(result.errors))
        return result
