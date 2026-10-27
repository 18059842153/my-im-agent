"""新闻推送模块的确定性测试（全部不联网，RSS 走 MockTransport）。"""

import json
from datetime import UTC, datetime, time, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from my_im_agent.config import Settings
from my_im_agent.news import (
    SECTIONS,
    AgentlyNewsSummarizer,
    EchoNewsSummarizer,
    FeedParseError,
    FeedSource,
    NewsDigestService,
    NewsFetcher,
    NewsItem,
    NewsScheduler,
    NewsState,
    NewsStateStore,
    NewsSummary,
    Section,
    SectionResult,
    decode_xml,
    dedupe_key,
    fallback_summaries,
    md_safe,
    parse_at,
    parse_feed,
    parse_published,
    reconcile,
    render_section,
    safe_url,
    select_items,
    should_fire,
    split_markdown,
)

NOW = datetime(2026, 10, 27, 9, 30, 0)
AT = time(9, 30)
CST = timezone(timedelta(hours=8))


def as_local(instant: datetime) -> datetime:
    """把绝对时刻换算成本地 naive 时间（测试不能假设跑在 +08:00）。"""
    return instant.astimezone().replace(tzinfo=None)


def make_settings(tmp_path: Path, **overrides) -> Settings:
    base = Settings(
        llm_api_key="",
        llm_model="test",
        data_dir=str(tmp_path),
        news_target_user="ChenKeCheng",
        news_top_n=15,
    )
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


def make_item(title: str, *, link: str = "", published: datetime | None = None, source="测试源") -> NewsItem:
    return NewsItem(
        id=f"id-{abs(hash(title)) % 100000}",
        title=title,
        link=link or f"https://example.com/{abs(hash(title)) % 100000}",
        source=source,
        published=published,
    )


RSS_UTF8 = (
    '<?xml version="1.0" encoding="utf-8"?>'
    "<rss version=\"2.0\"><channel><title>t</title>"
    "<item><title>IT之家新闻</title><link>https://example.com/a</link>"
    "<pubDate>Mon, 27 Oct 2026 09:00:00 +0800</pubDate></item>"
    "<item><title>第二条</title><link>https://example.com/b</link></item>"
    "</channel></rss>"
).encode()

RSS_GB2312 = (
    '<?xml version="1.0" encoding="gb2312"?>'
    "<rss version=\"2.0\"><channel>"
    "<item><title>中文标题</title><link>https://example.com/gb</link></item>"
    "</channel></rss>"
).encode("gb18030")

ATOM_FEED = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<feed xmlns="http://www.w3.org/2005/Atom">'
    "<title>atom</title>"
    "<entry>"
    "<title>Atom 条目</title>"
    '<link rel="self" href="https://example.com/self"/>'
    '<link rel="alternate" href="https://example.com/atom"/>'
    "<updated>2026-10-27T08:00:00+08:00</updated>"
    "</entry>"
    "</feed>"
).encode()


class TestParseFeed:
    def test_rss_utf8(self):
        items = parse_feed(RSS_UTF8, source="测试源")
        assert [i.title for i in items] == ["IT之家新闻", "第二条"]
        assert items[0].link == "https://example.com/a"
        assert items[0].published == as_local(datetime(2026, 10, 27, 9, 0, tzinfo=CST))

    def test_gb2312_declared(self):
        # 回归：xml.etree 遇到 gb2312 会抛 multi-byte encodings are not supported
        items = parse_feed(RSS_GB2312, source="测试源")
        assert items[0].title == "中文标题"

    def test_atom_uses_alternate_link(self):
        items = parse_feed(ATOM_FEED, source="测试源")
        assert items[0].title == "Atom 条目"
        assert items[0].link == "https://example.com/atom"
        assert items[0].published == as_local(datetime(2026, 10, 27, 8, 0, tzinfo=CST))

    def test_missing_pubdate(self):
        items = parse_feed(RSS_UTF8, source="测试源")
        assert items[1].published is None

    def test_html_is_rejected(self):
        with pytest.raises(FeedParseError):
            parse_feed(b"<html><body>not a feed</body></html>", source="测试源")

    def test_empty_feed_rejected(self):
        with pytest.raises(FeedParseError):
            parse_feed(b'<rss version="2.0"><channel></channel></rss>', source="测试源")

    def test_cdata_section(self):
        # CDATA 内不做实体解码，这是 XML 规范行为
        raw = (
            b'<?xml version="1.0"?><rss><channel><item>'
            b"<title><![CDATA[A & B]]></title><link>https://e.com/1</link>"
            b"</item></channel></rss>"
        )
        assert parse_feed(raw, source="s")[0].title == "A & B"

    def test_entities_decoded(self):
        raw = (
            b'<?xml version="1.0"?><rss><channel><item>'
            b"<title>A &amp; B</title><link>https://e.com/1</link>"
            b"</item></channel></rss>"
        )
        assert parse_feed(raw, source="s")[0].title == "A & B"

    def test_source_suffix_stripped(self):
        raw = (
            '<?xml version="1.0"?><rss><channel><item>'
            "<title>foo - IT之家</title><link>https://e.com/2</link>"
            "</item></channel></rss>"
        ).encode()
        assert parse_feed(raw, source="IT之家")[0].title == "foo"

    def test_non_http_link_dropped(self):
        raw = b"""<?xml version="1.0"?><rss><channel><item>
        <title>t</title><link>javascript:alert(1)</link>
        </item></channel></rss>"""
        assert parse_feed(raw, source="s")[0].link == ""

    def test_decode_xml_strips_declaration(self):
        text = decode_xml(b'<?xml version="1.0" encoding="utf-8"?><rss/>')
        assert not text.lstrip().startswith("<?xml")


class TestParsePublished:
    @pytest.mark.parametrize(
        ("raw", "instant"),
        [
            ("Mon, 27 Oct 2026 09:00:00 +0800", datetime(2026, 10, 27, 9, 0, tzinfo=CST)),
            ("Mon, 27 Oct 2026 01:00:00 GMT", datetime(2026, 10, 27, 1, 0, tzinfo=UTC)),
            ("2026-10-27T09:00:00+08:00", datetime(2026, 10, 27, 9, 0, tzinfo=CST)),
            ("2026-10-27T01:00:00Z", datetime(2026, 10, 27, 1, 0, tzinfo=UTC)),
        ],
    )
    def test_parses_common_formats(self, raw, instant):
        # 不管测试机在哪个时区，都换算成同一绝对时刻
        assert parse_published(raw) == as_local(instant)

    def test_naive_kept_as_is(self):
        assert parse_published("Mon, 27 Oct 2026 09:00:00") == datetime(2026, 10, 27, 9, 0)

    def test_cst_treated_as_china_time(self):
        # email 库会把 CST 当成美国中部时间（-06:00），这里必须纠正为 +08:00
        assert parse_published("Mon, 27 Oct 2026 09:00:00 CST") == as_local(
            datetime(2026, 10, 27, 9, 0, tzinfo=CST)
        )

    def test_garbage_returns_none(self):
        assert parse_published("昨天下午") is None
        assert parse_published("") is None
        assert parse_published(None) is None


def make_transport(routes: dict[str, httpx.Response]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return routes.get(str(request.url), httpx.Response(404))

    return httpx.MockTransport(handler)


SECTION = Section("tech", "科技", (FeedSource("A", "https://a.test/rss"), FeedSource("B", "https://b.test/rss")))


class TestFetcher:
    @pytest.mark.asyncio
    async def test_merges_and_sorts(self):
        routes = {
            "https://a.test/rss": httpx.Response(200, content=RSS_UTF8),
            "https://b.test/rss": httpx.Response(200, content=RSS_UTF8),
        }
        fetcher = NewsFetcher(transport=make_transport(routes))
        result = await fetcher.fetch(SECTION)
        await fetcher.aclose()

        assert len(result.items) == 4
        assert result.errors == []
        assert result.items[0].published == as_local(datetime(2026, 10, 27, 9, 0, tzinfo=CST))

    @pytest.mark.asyncio
    async def test_one_source_down_does_not_break_other(self):
        routes = {
            "https://a.test/rss": httpx.Response(200, content=RSS_UTF8),
            "https://b.test/rss": httpx.Response(503),
        }
        fetcher = NewsFetcher(transport=make_transport(routes))
        result = await fetcher.fetch(SECTION)
        await fetcher.aclose()

        assert len(result.items) == 2
        assert len(result.errors) == 1
        assert "HTTP 503" in result.errors[0]

    @pytest.mark.asyncio
    async def test_all_sources_down_returns_empty(self):
        fetcher = NewsFetcher(transport=make_transport({}))
        result = await fetcher.fetch(SECTION)
        await fetcher.aclose()

        assert result.items == []
        assert len(result.errors) == 2


class TestSelect:
    def test_dedupes_same_title_across_sources(self):
        items = [make_item("同一条新闻", link="https://a.com/1"), make_item("同一条新闻", link="https://b.com/2")]
        picked, keys = select_items(items, limit=15)
        assert len(picked) == 1
        assert len(keys) == 1

    def test_honours_limit(self):
        items = [make_item(f"标题{i}") for i in range(25)]
        picked, _ = select_items(items, limit=15)
        assert len(picked) == 15

    def test_skips_seen(self):
        item = make_item("旧的")
        picked, _ = select_items([item], limit=15, seen={dedupe_key(item)})
        assert picked == []

    def test_sorted_by_published_desc(self):
        items = [
            make_item("早", published=datetime(2026, 10, 27, 7, 0)),
            make_item("晚", published=datetime(2026, 10, 27, 9, 0)),
            make_item("无时间", published=None),
        ]
        picked, _ = select_items(items, limit=15)
        assert [i.title for i in picked] == ["晚", "早", "无时间"]

    def test_returns_matching_keys(self):
        items = [make_item("a"), make_item("b")]
        picked, keys = select_items(items, limit=15)
        assert keys == [dedupe_key(i) for i in picked]


class TestReconcile:
    def test_reorders_by_input_and_fills_missing(self):
        items = [make_item("甲"), make_item("乙"), make_item("丙")]
        summaries = [
            NewsSummary(id=items[2].id, title="丙", summary="丙的摘要"),
            NewsSummary(id=items[0].id, title="甲", summary="甲的摘要"),
        ]
        pairs = reconcile(items, summaries)

        assert [pair[0].title for pair in pairs] == ["甲", "乙", "丙"]
        assert pairs[1][1].summary == ""
        assert pairs[1][1].title == "乙"

    def test_unknown_and_duplicate_ids(self):
        items = [make_item("甲")]
        summaries = [
            NewsSummary(id="鬼影", title="伪造", summary="不该出现"),
            NewsSummary(id=items[0].id, title="甲", summary="第一次"),
            NewsSummary(id=items[0].id, title="甲", summary="第二次"),
        ]
        pairs = reconcile(items, summaries)
        assert len(pairs) == 1
        assert pairs[0][1].summary == "第一次"

    def test_summary_newlines_collapsed(self):
        items = [make_item("甲")]
        pairs = reconcile(items, [NewsSummary(id=items[0].id, title="甲", summary="第一行\n第二行")])
        assert pairs[0][1].summary == "第一行 第二行"


class FakeSummarizer:
    def __init__(self, model, *, chunk_size=8, fail_chunks=()):
        self.model = model
        self.chunk_size = chunk_size
        self.fail_chunks = fail_chunks
        self.calls: list[list[str]] = []

    async def summarize(self, *, section_label, items, now):
        chunks = [items[i : i + self.chunk_size] for i in range(0, len(items), self.chunk_size)]
        out = []
        for index, chunk in enumerate(chunks):
            self.calls.append([i.id for i in chunk])
            if index in self.fail_chunks:
                out.extend(fallback_summaries(chunk))
                continue
            out.extend(NewsSummary(id=i.id, title=i.title, summary=self.model) for i in chunk)
        return out


class TestSummarizer:
    @pytest.mark.asyncio
    async def test_echo_returns_titles_only(self):
        items = [make_item("甲"), make_item("乙")]
        summaries = await EchoNewsSummarizer().summarize(section_label="科技", items=items, now=NOW)
        assert [s.summary for s in summaries] == ["", ""]

    @pytest.mark.asyncio
    async def test_chunking_splits_requests(self):
        settings = make_settings(Path("."), llm_api_key="k")
        summarizer = AgentlyNewsSummarizer(settings, chunk_size=8)
        # 只验证分块逻辑，不真的请求模型：直接打桩内部方法
        seen: list[int] = []

        async def fake_chunk(section_label, chunk, now):
            seen.append(len(chunk))
            return [NewsSummary(id=i.id, title=i.title, summary="s") for i in chunk]

        summarizer._summarize_chunk = fake_chunk  # type: ignore[method-assign]
        items = [make_item(f"t{i}") for i in range(20)]
        result = await summarizer.summarize(section_label="科技", items=items, now=NOW)

        assert seen == [8, 8, 4]
        assert len(result) == 20


class TestRender:
    def test_sanitises_markdown(self):
        # 严格断言：会被企微语法识别的字符必须清掉
        cleaned = md_safe("*a_b[c](d)#e>f`g`")
        # 严格断言：会被企微语法识别的字符必须清掉
        cleaned = md_safe("*a_b[c](d)#e>f`g`")
        for char in "*_[#>`":
            assert char not in cleaned

    def test_url_encoded(self):
        assert safe_url("https://e.com/a b(1)").endswith("a%20b%281%29")

    def test_render_inlines_link_on_title(self):
        pairs = reconcile([make_item("标题一", link="https://e.com/1")], [])
        text = render_section(label="科技", now=NOW, pairs=pairs)
        assert "1. **[标题一](https://e.com/1)**" in text
        assert "科技" in text
        assert "阅读原文" not in text  # 内联后不再单独占一行，省字节

    def test_split_respects_byte_limit(self):
        pairs = reconcile(
            [make_item(f"标题{i}", link=f"https://e.com/{i}") for i in range(30)],
            [],
        )
        text = render_section(label="科技", now=NOW, pairs=pairs)
        chunks = split_markdown(text, limit=600)

        assert len(chunks) >= 2
        for chunk in chunks:
            assert len(chunk.encode("utf-8")) <= 600
        assert chunks[1].startswith("（续 2/")

    def test_hard_split_keeps_all_chars(self):
        line = "很长的行" * 200
        chunks = split_markdown(line, limit=300)
        joined = "".join(chunks)
        assert joined.count("行") >= 200
        for chunk in chunks:
            assert len(chunk.encode("utf-8")) <= 300


class TestState:
    def test_not_yet(self):
        fire, reason = should_fire(NewsState(), datetime(2026, 10, 27, 9, 29), at=AT)
        assert (fire, reason) == (False, "not-yet")

    def test_due_at_slot(self):
        assert should_fire(NewsState(), NOW, at=AT)[0] is True

    def test_within_catchup(self):
        fire, _ = should_fire(NewsState(), datetime(2026, 10, 27, 10, 45), at=AT)
        assert fire is True

    def test_expired_after_window(self):
        fire, reason = should_fire(NewsState(), datetime(2026, 10, 27, 11, 30), at=AT)
        assert (fire, reason) == (False, "expired")

    @pytest.mark.parametrize("status", ["ok", "partial", "sending"])
    def test_already_sent_today(self, status):
        state = NewsState(last_date="2026-10-27", last_status=status)
        assert should_fire(state, NOW, at=AT)[0] is False

    def test_gives_up_after_max_attempts(self):
        state = NewsState(
            last_date="2026-10-27",
            last_status="failed",
            attempt_date="2026-10-27",
            attempts=3,
        )
        assert should_fire(state, NOW, at=AT)[0] is False

    def test_failed_allows_retry(self):
        state = NewsState(
            last_date="2026-10-27",
            last_status="failed",
            attempt_date="2026-10-27",
            attempts=1,
        )
        assert should_fire(state, NOW, at=AT)[0] is True

    def test_store_roundtrip_and_atomic_write(self, tmp_path):
        store = NewsStateStore(tmp_path / "news_state.json")
        store.mark_attempt(NOW)
        store.mark_done(NOW, ["k1"], status="ok")

        loaded = store.load()
        assert loaded.last_status == "ok"
        assert loaded.seen == {"k1": "2026-10-27"}
        assert not (tmp_path / "news_state.json.tmp").exists()

    def test_corrupted_state_falls_back(self, tmp_path):
        path = tmp_path / "news_state.json"
        path.write_text("not json", encoding="utf-8")
        assert NewsStateStore(path).load().last_status == "none"

    def test_prune_seen_keeps_recent(self, tmp_path):
        store = NewsStateStore(tmp_path / "news_state.json")
        store.save(NewsState(seen={"old": "2026-10-01"}))
        store.mark_done(NOW, ["new"], status="ok")
        assert store.load().seen == {"new": "2026-10-27"}

    def test_parse_at_falls_back(self):
        assert parse_at("09:15") == time(9, 15)
        assert parse_at("乱写") == time(9, 30)


class FakeFetcher:
    def __init__(self, results: dict[str, SectionResult]):
        self.results = results

    async def fetch(self, section: Section) -> SectionResult:
        return self.results[section.key]

    async def aclose(self) -> None:
        return None


def section_result(key: str, items: list[NewsItem], *, errors: list[str] | None = None) -> SectionResult:
    section = next(s for s in SECTIONS if s.key == key)
    return SectionResult(section=section, items=items, errors=errors or [])


def make_service(tmp_path, *, fetcher, summarizer=None, sent=None, settings=None):
    settings = settings or make_settings(tmp_path)
    state = NewsStateStore(tmp_path / "news_state.json")
    sent = sent if sent is not None else []

    async def notify(user_id, conversation_id, text):
        sent.append(text)

    service = NewsDigestService(
        settings=settings,
        state=state,
        fetcher=fetcher,
        summarizer=summarizer or FakeSummarizer("摘要"),
        notify=notify,
    )
    return service, state, sent


class TestService:
    @pytest.mark.asyncio
    async def test_sends_two_sections(self, tmp_path):
        fetcher = FakeFetcher(
            {
                "tech": section_result("tech", [make_item(f"科技{i}") for i in range(3)]),
                "general": section_result("general", [make_item(f"综合{i}") for i in range(2)]),
            }
        )
        service, state, sent = make_service(tmp_path, fetcher=fetcher)

        result = await service.deliver(NOW)

        assert result.status == "ok"
        assert len(sent) == 2
        assert "科技" in sent[0] and "综合" in sent[1]
        assert state.load().last_status == "ok"

    @pytest.mark.asyncio
    async def test_one_section_empty_sends_one(self, tmp_path):
        fetcher = FakeFetcher(
            {
                "tech": section_result("tech", [make_item("科技1")]),
                "general": section_result("general", []),
            }
        )
        service, _, sent = make_service(tmp_path, fetcher=fetcher)

        result = await service.deliver(NOW)

        assert result.status == "ok"
        assert len(sent) == 1

    @pytest.mark.asyncio
    async def test_all_empty_sends_nothing(self, tmp_path):
        fetcher = FakeFetcher(
            {"tech": section_result("tech", []), "general": section_result("general", [])}
        )
        service, state, sent = make_service(tmp_path, fetcher=fetcher)

        result = await service.deliver(NOW)

        assert sent == []
        assert result.status == "failed"
        assert state.load().last_status == "failed"

    @pytest.mark.asyncio
    async def test_notify_failure_does_not_raise(self, tmp_path):
        fetcher = FakeFetcher(
            {
                "tech": section_result("tech", [make_item("科技1")]),
                "general": section_result("general", [make_item("综合1")]),
            }
        )
        settings = make_settings(tmp_path)
        state = NewsStateStore(tmp_path / "news_state.json")

        async def boom(user_id, conversation_id, text):
            raise RuntimeError("推送通道断了")

        service = NewsDigestService(
            settings=settings,
            state=state,
            fetcher=fetcher,
            summarizer=FakeSummarizer("s"),
            notify=boom,
        )

        result = await service.deliver(NOW)

        assert result.status == "failed"
        assert state.load().last_status == "failed"

    @pytest.mark.asyncio
    async def test_partial_when_second_send_fails(self, tmp_path):
        fetcher = FakeFetcher(
            {
                "tech": section_result("tech", [make_item("科技1")]),
                "general": section_result("general", [make_item("综合1")]),
            }
        )
        settings = make_settings(tmp_path)
        state = NewsStateStore(tmp_path / "news_state.json")
        calls = {"n": 0}

        async def flaky(user_id, conversation_id, text):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("第二条失败")

        service = NewsDigestService(
            settings=settings,
            state=state,
            fetcher=fetcher,
            summarizer=FakeSummarizer("s"),
            notify=flaky,
        )

        result = await service.deliver(NOW)

        assert result.status == "partial"
        assert state.load().last_status == "partial"

    @pytest.mark.asyncio
    async def test_skips_when_already_sent(self, tmp_path):
        fetcher = FakeFetcher(
            {
                "tech": section_result("tech", [make_item("x")]),
                "general": section_result("general", []),
            }
        )
        service, state, sent = make_service(tmp_path, fetcher=fetcher)

        await service.deliver(NOW)
        sent.clear()
        result = await service.deliver(NOW + timedelta(minutes=5))

        assert sent == []
        assert result.status == "already-sent"

    @pytest.mark.asyncio
    async def test_dry_run_does_not_persist(self, tmp_path):
        fetcher = FakeFetcher(
            {
                "tech": section_result("tech", [make_item("x")]),
                "general": section_result("general", []),
            }
        )
        service, state, sent = make_service(tmp_path, fetcher=fetcher)

        result = await service.deliver(NOW, force=True, persist=False)

        assert result.status == "ok"
        assert len(sent) == 1
        assert state.load().last_status == "none"

    @pytest.mark.asyncio
    async def test_expired_marks_skipped(self, tmp_path):
        fetcher = FakeFetcher({"tech": section_result("tech", []), "general": section_result("general", [])})
        service, state, sent = make_service(tmp_path, fetcher=fetcher)

        result = await service.deliver(datetime(2026, 10, 27, 14, 0))

        assert result.status == "expired"
        assert sent == []
        assert state.load().last_status == "skipped"


class TestScheduler:
    @pytest.mark.asyncio
    async def test_tick_fires_once(self, tmp_path):
        fetcher = FakeFetcher(
            {
                "tech": section_result("tech", [make_item("x")]),
                "general": section_result("general", []),
            }
        )
        service, _, sent = make_service(tmp_path, fetcher=fetcher)
        scheduler = NewsScheduler(service, make_settings(tmp_path))

        await scheduler._tick(NOW)
        await scheduler._tick(NOW)

        assert len(sent) == 1

    @pytest.mark.asyncio
    async def test_tick_before_slot_noop(self, tmp_path):
        fetcher = FakeFetcher(
            {
                "tech": section_result("tech", [make_item("x")]),
                "general": section_result("general", []),
            }
        )
        service, _, sent = make_service(tmp_path, fetcher=fetcher)
        scheduler = NewsScheduler(service, make_settings(tmp_path))

        await scheduler._tick(datetime(2026, 10, 27, 8, 0))

        assert sent == []

    @pytest.mark.asyncio
    async def test_disabled_noop(self, tmp_path):
        fetcher = FakeFetcher(
            {
                "tech": section_result("tech", [make_item("x")]),
                "general": section_result("general", []),
            }
        )
        service, _, sent = make_service(tmp_path, fetcher=fetcher)
        scheduler = NewsScheduler(service, make_settings(tmp_path, news_enabled=False))

        await scheduler._tick(NOW)

        assert sent == []

    @pytest.mark.asyncio
    async def test_loop_survives_service_error(self, tmp_path):
        class BrokenService:
            async def deliver(self, now, *, late=False):  # noqa: ANN001
                raise RuntimeError("炸了")

        settings = make_settings(tmp_path)
        scheduler = NewsScheduler(BrokenService(), settings, interval=0.01)
        task = __import__("asyncio").create_task(scheduler.start())
        await __import__("asyncio").sleep(0.05)
        scheduler.stop()
        task.cancel()
        try:
            await task
        except __import__("asyncio").CancelledError:
            pass


class TestConfigBool:
    def test_as_bool(self):
        from my_im_agent.config import _as_bool

        assert _as_bool("0", True) is False
        assert _as_bool("1", False) is True
        assert _as_bool("off", True) is False
        assert _as_bool("", True) is True
        assert _as_bool("乱写", False) is False


class TestSettingsDefaults:
    def test_news_defaults(self):
        settings = Settings()
        assert settings.news_at == "09:30"
        assert settings.news_top_n == 15
        assert settings.news_max_bytes == 3800
        assert settings.news_configured is True

    def test_require_news_rejects_empty_target(self):
        with pytest.raises(RuntimeError, match="NEWS_TARGET_USER"):
            Settings(news_target_user="").require_news()

    def test_state_json_roundtrip_shape(self, tmp_path):
        store = NewsStateStore(tmp_path / "state.json")
        store.mark_done(NOW, ["k"], status="ok")
        raw = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
        assert set(raw) >= {"last_date", "last_status", "seen", "attempts"}
