from datetime import datetime, timedelta
from pathlib import Path

import pytest

from my_im_agent.alarms import Alarm, AlarmScheduler, AlarmStore, next_run

NOW = datetime(2026, 10, 23, 17, 0, 0)  # 星期五


def make_alarm(tmp_path: Path, **kwargs) -> Alarm:
    store = AlarmStore(tmp_path / "alarms.json")
    defaults = {
        "conversation_id": "c1",
        "user_id": "u1",
        "run_at": NOW,
        "message": "开会",
    }
    defaults.update(kwargs)
    return store.add(**defaults)


class TestAlarmStore:
    def test_add_assigns_incrementing_ids(self, tmp_path):
        store = AlarmStore(tmp_path / "alarms.json")
        a1 = store.add(conversation_id="c1", user_id="u1", run_at=NOW, message="m1")
        a2 = store.add(conversation_id="c2", user_id="u2", run_at=NOW, message="m2")
        assert (a1.id, a2.id) == (1, 2)

    def test_list_is_filtered_and_sorted(self, tmp_path):
        store = AlarmStore(tmp_path / "alarms.json")
        store.add(conversation_id="c1", user_id="u1", run_at=NOW + timedelta(hours=2), message="晚")
        store.add(conversation_id="c1", user_id="u1", run_at=NOW + timedelta(hours=1), message="早")
        store.add(conversation_id="c2", user_id="u2", run_at=NOW, message="别人")

        items = store.list("c1")

        assert [a.message for a in items] == ["早", "晚"]

    def test_remove_only_within_same_conversation(self, tmp_path):
        store = AlarmStore(tmp_path / "alarms.json")
        store.add(conversation_id="c1", user_id="u1", run_at=NOW, message="m")

        assert not store.remove("c2", 1)  # 别人的会话删不到
        assert store.remove("c1", 1)
        assert store.list() == []

    def test_persistence_round_trip(self, tmp_path):
        path = tmp_path / "alarms.json"
        store = AlarmStore(path)
        store.add(
            conversation_id="c1",
            user_id="u1",
            run_at=NOW,
            message="喝水",
            repeat="daily",
        )

        reloaded = AlarmStore(path)

        assert len(reloaded.list()) == 1
        alarm = reloaded.list()[0]
        assert alarm.message == "喝水"
        assert alarm.repeat == "daily"
        assert alarm.run_at == NOW

    def test_corrupted_file_starts_empty(self, tmp_path):
        path = tmp_path / "alarms.json"
        path.write_text("not json", encoding="utf-8")
        store = AlarmStore(path)
        assert store.list() == []

    def test_due_filters_future(self, tmp_path):
        store = AlarmStore(tmp_path / "alarms.json")
        store.add(conversation_id="c1", user_id="u1", run_at=NOW - timedelta(minutes=1), message="已到点")
        store.add(conversation_id="c1", user_id="u1", run_at=NOW + timedelta(hours=1), message="还没到")

        due = store.due(NOW)

        assert [a.message for a in due] == ["已到点"]

    def test_unknown_repeat_rejected(self, tmp_path):
        store = AlarmStore(tmp_path / "alarms.json")
        with pytest.raises(ValueError):
            store.add(conversation_id="c1", user_id="u1", run_at=NOW, message="m", repeat="hourly")


class TestNextRun:
    def test_daily(self):
        assert next_run(NOW, "daily", NOW) == NOW + timedelta(days=1)

    def test_weekly(self):
        assert next_run(NOW, "weekly", NOW) == NOW + timedelta(days=7)

    def test_workdays_skips_weekend(self):
        # 周五 -> 下周一
        nxt = next_run(NOW, "workdays", NOW)
        assert nxt == NOW + timedelta(days=3)
        assert nxt.weekday() == 0

    def test_none_returns_none(self):
        assert next_run(NOW, "none", NOW) is None

    def test_expired_repeat_advances_into_future(self):
        # 重启后：原触发时间已过去 10 天，应推进到未来最近的每天同刻
        old = NOW - timedelta(days=10)
        nxt = next_run(old, "daily", NOW)
        assert nxt > NOW
        assert nxt.time() == old.time()

    def test_unknown_repeat_raises(self):
        with pytest.raises(ValueError):
            next_run(NOW, "monthly", NOW)


class TestAlarmScheduler:
    @pytest.mark.asyncio
    async def test_fires_due_alarm_and_removes_one_shot(self, tmp_path):
        store = AlarmStore(tmp_path / "alarms.json")
        store.add(conversation_id="c1", user_id="u1", run_at=NOW, message="开会")
        fired: list[tuple[str, str, str]] = []

        async def notify(user_id: str, conv: str, text: str) -> None:
            fired.append((user_id, conv, text))

        scheduler = AlarmScheduler(store, notify)
        await scheduler._tick(NOW)

        assert fired == [("u1", "c1", "⏰ 闹钟提醒：开会")]
        assert store.list() == []

    @pytest.mark.asyncio
    async def test_repeat_alarm_advances_after_fire(self, tmp_path):
        store = AlarmStore(tmp_path / "alarms.json")
        store.add(conversation_id="c1", user_id="u1", run_at=NOW, message="站会", repeat="daily")
        fired: list[str] = []

        async def notify(user_id: str, conv: str, text: str) -> None:
            fired.append(text)

        scheduler = AlarmScheduler(store, notify)
        await scheduler._tick(NOW)

        assert len(fired) == 1
        assert store.list()[0].run_at == NOW + timedelta(days=1)

    @pytest.mark.asyncio
    async def test_notify_failure_still_advances(self, tmp_path):
        store = AlarmStore(tmp_path / "alarms.json")
        store.add(conversation_id="c1", user_id="u1", run_at=NOW, message="m", repeat="daily")

        async def bad_notify(user_id: str, conv: str, text: str) -> None:
            raise RuntimeError("network down")

        scheduler = AlarmScheduler(store, bad_notify)
        await scheduler._tick(NOW)  # 不抛异常

        assert store.list()[0].run_at == NOW + timedelta(days=1)

    @pytest.mark.asyncio
    async def test_catch_up_fires_recently_missed(self, tmp_path):
        store = AlarmStore(tmp_path / "alarms.json")
        store.add(
            conversation_id="c1",
            user_id="u1",
            run_at=datetime.now() - timedelta(minutes=3),
            message="刚错过",
        )
        fired: list[str] = []

        async def notify(user_id: str, conv: str, text: str) -> None:
            fired.append(text)

        scheduler = AlarmScheduler(store, notify)
        await scheduler._catch_up()

        assert len(fired) == 1
        assert store.list() == []

    @pytest.mark.asyncio
    async def test_catch_up_drops_stale_one_shot(self, tmp_path):
        store = AlarmStore(tmp_path / "alarms.json")
        store.add(
            conversation_id="c1",
            user_id="u1",
            run_at=datetime.now() - timedelta(hours=3),
            message="早就过了",
        )
        fired: list[str] = []

        async def notify(user_id: str, conv: str, text: str) -> None:
            fired.append(text)

        scheduler = AlarmScheduler(store, notify)
        await scheduler._catch_up()

        assert fired == []
        assert store.list() == []

    @pytest.mark.asyncio
    async def test_catch_up_advances_stale_repeat(self, tmp_path):
        store = AlarmStore(tmp_path / "alarms.json")
        old = datetime.now() - timedelta(days=2)
        store.add(conversation_id="c1", user_id="u1", run_at=old, message="每天", repeat="daily")
        fired: list[str] = []

        async def notify(user_id: str, conv: str, text: str) -> None:
            fired.append(text)

        scheduler = AlarmScheduler(store, notify)
        await scheduler._catch_up()

        assert fired == []  # 过期太久的 repeat 不补发
        assert store.list()[0].run_at > datetime.now()
