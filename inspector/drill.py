"""单场演练：独立时钟、独立存储、独立调度器。

约束：
- 整个服务同一时刻只允许一场演练；
- 每场从空的演练记录开始（内存 SQLite，进程重启即消失）；
- 初始巡检时刻取自正式设置；
- 演练的计划、结果、设置都只写入本场存储，不碰正式库；
- 退出立即生效：调用调度器的非阻塞 ``cancel()``，晚到的读盘结果丢弃。
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta
from typing import Callable, Optional

from . import timeutil
from .scheduler import Scheduler, real_utc_now
from .storage import Storage


class DrillAlreadyActiveError(RuntimeError):
    """已有演练在进行中，整个服务一次只能开一场。"""


class DrillClock:
    """演练时钟：真实时间 + 本场偏移。偏移只存在于本场内存中。"""

    def __init__(self, real_clock: Callable[[], datetime] = real_utc_now):
        self._real_clock = real_clock
        self._offset = 0.0
        self.lock = threading.Lock()

    def __call__(self) -> datetime:
        with self.lock:
            return self._real_clock() + timedelta(seconds=self._offset)

    def offset_seconds(self) -> float:
        with self.lock:
            return self._offset

    def set_to(self, dt_utc: datetime) -> None:
        if dt_utc.tzinfo is not None:
            dt_utc = dt_utc.astimezone(timeutil.UTC).replace(tzinfo=None)
        with self.lock:
            self._offset = (dt_utc - self._real_clock()).total_seconds()

    def advance(self, seconds: float) -> None:
        with self.lock:
            self._offset += seconds

    def reset(self) -> None:
        with self.lock:
            self._offset = 0.0


class DrillSession:
    """一场演练的运行时状态。"""

    def __init__(self, storage: Storage, clock: DrillClock,
                 scheduler: Scheduler, started_real_utc: datetime,
                 check_time: str):
        self.storage = storage
        self.clock = clock
        self.scheduler = scheduler
        self.started_real_utc = started_real_utc
        self.check_time = check_time

    def start(self) -> None:
        self.scheduler.start()
        self.scheduler.wake()

    def close(self) -> None:
        # 非阻塞：读盘还在进行时也能立即退出，晚到结果由调度器丢弃。
        self.scheduler.cancel()

    def status(self) -> dict:
        st = self.scheduler.status()
        st["clock_offset_seconds"] = self.clock.offset_seconds()
        st["started_real_utc"] = self.started_real_utc
        return st


class DrillManager:
    """管理唯一一场演练的生命周期。"""

    def __init__(self,
                 production_storage: Storage,
                 scheduler_factory: Callable[[Storage, Callable[[], datetime]],
                                             Scheduler],
                 real_clock: Callable[[], datetime] = real_utc_now,
                 storage_factory: Callable[[], Storage] = lambda: Storage(":memory:")):
        self.production_storage = production_storage
        self._scheduler_factory = scheduler_factory
        self._real_clock = real_clock
        self._storage_factory = storage_factory
        self.lock = threading.RLock()
        self._session: Optional[DrillSession] = None

    def current(self) -> Optional[DrillSession]:
        with self.lock:
            return self._session

    def start(self) -> DrillSession:
        with self.lock:
            if self._session is not None:
                raise DrillAlreadyActiveError("已有演练在进行中")
            storage = self._storage_factory()
            # 每场从空的演练记录开始；初始巡检时刻取正式设置。
            check_time = self.production_storage.get_setting("check_time")
            storage.set_setting("check_time", check_time)
            clock = DrillClock(self._real_clock)
            scheduler = self._scheduler_factory(storage, clock)
            session = DrillSession(
                storage=storage,
                clock=clock,
                scheduler=scheduler,
                started_real_utc=self._real_clock(),
                check_time=check_time,
            )
            self._session = session
        session.start()
        return session

    def exit(self) -> bool:
        """退出演练；返回是否确实退出了一场。立即生效，不等待读盘。"""
        with self.lock:
            session = self._session
            self._session = None
        if session is None:
            return False
        session.close()
        return True
