"""闹钟的宿主侧职责：存储、调度、触发。

- 时间计算、重复规则推进、到点判定全部是 host 代码（确定性逻辑）。
- 模型只负责把「明天早上9点」这种自然语言翻译成 ISO8601，由 host 校验后才入库。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

REPEAT_RULES = ("none", "daily", "weekly", "workdays")

# 重启后补偿窗口：错过不超过该时长的闹钟补发一次
CATCH_UP_GRACE = timedelta(minutes=10)
# 最远允许设置的闹钟
MAX_FUTURE = timedelta(days=366)

NotifyFn = Callable[[str, str, str], Awaitable[None]]  # (user_id, conversation_id, text)


@dataclass
class Alarm:
    id: int
    conversation_id: str
    user_id: str
    run_at: datetime  # 本地时间，naive
    message: str
    repeat: str = "none"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "conversation_id": self.conversation_id,
            "user_id": self.user_id,
            "run_at": self.run_at.isoformat(timespec="seconds"),
            "message": self.message,
            "repeat": self.repeat,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Alarm:
        return cls(
            id=int(data["id"]),
            conversation_id=str(data["conversation_id"]),
            user_id=str(data["user_id"]),
            run_at=datetime.fromisoformat(data["run_at"]),
            message=str(data["message"]),
            repeat=str(data.get("repeat", "none")),
        )


def next_run(run_at: datetime, repeat: str, now: datetime) -> datetime | None:
    """按重复规则推进到下一个触发时间；none 返回 None。"""
    if repeat == "none":
        return None
    if repeat == "daily":
        candidate = run_at + timedelta(days=1)
    elif repeat == "weekly":
        candidate = run_at + timedelta(days=7)
    elif repeat == "workdays":
        candidate = run_at + timedelta(days=1)
        while candidate.weekday() >= 5:  # 5=周六 6=周日
            candidate += timedelta(days=1)
    else:
        raise ValueError(f"unknown repeat rule: {repeat}")
    # 保证推进结果一定在未来（应对重启后 repeat 闹钟已过期的情况）
    while candidate <= now:
        candidate = next_run(candidate, repeat, candidate) or candidate + timedelta(days=1)
    return candidate


class AlarmStore:
    """JSON 文件持久化，原子写。进程内用一把锁保证并发安全。"""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._alarms: list[Alarm] = []
        self._load()

    # --- 持久化 ---

    def _load(self) -> None:
        if not self._path.is_file():
            self._alarms = []
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            self._alarms = [Alarm.from_dict(item) for item in raw]
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            logger.warning("alarms file %s is corrupted, starting empty: %s", self._path, exc)
            self._alarms = []

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(
            json.dumps([a.to_dict() for a in self._alarms], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, self._path)

    # --- 查询 / 修改（调用方自行保证在锁外读到的列表不会被并发修改，返回副本） ---

    def list(self, conversation_id: str | None = None) -> list[Alarm]:
        with self._lock:
            items = self._alarms
            if conversation_id is not None:
                items = [a for a in items if a.conversation_id == conversation_id]
            return sorted(items, key=lambda a: a.run_at)

    def get(self, conversation_id: str, alarm_id: int) -> Alarm | None:
        with self._lock:
            for alarm in self._alarms:
                if alarm.conversation_id == conversation_id and alarm.id == alarm_id:
                    return alarm
            return None

    def add(
        self,
        *,
        conversation_id: str,
        user_id: str,
        run_at: datetime,
        message: str,
        repeat: str = "none",
    ) -> Alarm:
        if repeat not in REPEAT_RULES:
            raise ValueError(f"repeat must be one of {REPEAT_RULES}")
        with self._lock:
            alarm_id = max((a.id for a in self._alarms), default=0) + 1
            alarm = Alarm(
                id=alarm_id,
                conversation_id=conversation_id,
                user_id=user_id,
                run_at=run_at.replace(microsecond=0),
                message=message,
                repeat=repeat,
            )
            self._alarms.append(alarm)
            self._save()
            return alarm

    def remove(self, conversation_id: str, alarm_id: int) -> bool:
        with self._lock:
            before = len(self._alarms)
            self._alarms = [
                a
                for a in self._alarms
                if not (a.conversation_id == conversation_id and a.id == alarm_id)
            ]
            changed = len(self._alarms) != before
            if changed:
                self._save()
            return changed

    def due(self, now: datetime) -> list[Alarm]:
        with self._lock:
            return [a for a in self._alarms if a.run_at <= now]

    def update(self, alarm: Alarm) -> None:
        """就地更新（调度器推进 run_at 后调用）。"""
        with self._lock:
            for i, existing in enumerate(self._alarms):
                if existing.id == alarm.id and existing.conversation_id == alarm.conversation_id:
                    self._alarms[i] = alarm
                    self._save()
                    return
            raise KeyError(f"alarm #{alarm.id} not found in {alarm.conversation_id}")


class AlarmScheduler:
    """轻量调度器：每秒扫一次到点闹钟并触发通知。"""

    def __init__(
        self,
        store: AlarmStore,
        notify: NotifyFn,
        *,
        interval: float = 1.0,
        catch_up_grace: timedelta = CATCH_UP_GRACE,
    ) -> None:
        self._store = store
        self._notify = notify
        self._interval = interval
        self._grace = catch_up_grace
        self._task: asyncio.Task | None = None
        self._stopped = asyncio.Event()

    async def start(self) -> None:
        """启动调度循环；先做重启补偿，再进入 tick。"""
        await self._catch_up()
        self._stopped.clear()
        try:
            while not self._stopped.is_set():
                await self._tick(datetime.now())
                try:
                    await asyncio.wait_for(self._stopped.wait(), timeout=self._interval)
                except TimeoutError:
                    pass
        except asyncio.CancelledError:
            raise

    def stop(self) -> None:
        self._stopped.set()

    async def _catch_up(self) -> None:
        """重启补偿：错过时间不久的补发，太久的推进或丢弃。"""
        now = datetime.now()
        for alarm in self._store.list():
            if alarm.run_at > now:
                continue
            if now - alarm.run_at <= self._grace:
                await self._fire(alarm, now)
            else:
                if alarm.repeat != "none":
                    alarm.run_at = next_run(alarm.run_at, alarm.repeat, now)  # type: ignore[assignment]
                    self._store.update(alarm)
                else:
                    self._store.remove(alarm.conversation_id, alarm.id)
                    logger.info("drop expired one-shot alarm #%s", alarm.id)

    async def _tick(self, now: datetime) -> None:
        for alarm in self._store.due(now):
            await self._fire(alarm, now)

    async def _fire(self, alarm: Alarm, now: datetime) -> None:
        try:
            await self._notify(alarm.user_id, alarm.conversation_id, f"⏰ 闹钟提醒：{alarm.message}")
        except Exception:
            logger.exception("failed to notify alarm #%s", alarm.id)
        nxt = next_run(alarm.run_at, alarm.repeat, now)
        if nxt is None:
            self._store.remove(alarm.conversation_id, alarm.id)
        else:
            alarm.run_at = nxt
            self._store.update(alarm)
