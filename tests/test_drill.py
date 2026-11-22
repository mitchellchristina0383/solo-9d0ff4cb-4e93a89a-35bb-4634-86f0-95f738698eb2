"""演练生命周期与隔离性测试。"""
import os
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone

from inspector.drill import DrillAlreadyActiveError, DrillManager
from inspector.scheduler import Scheduler
from inspector.storage import Storage

UTC = timezone.utc


class MutableClock:
    def __init__(self, start_utc: datetime):
        self.now = start_utc.astimezone(UTC).replace(tzinfo=None)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


class BlockingReader:
    def __init__(self, value=4242, auto_release=False):
        self.value = value
        self.auto_release = auto_release
        self.entered = threading.Event()
        self.release = threading.Event()
        if auto_release:
            self.release.set()
        self.calls = 0

    def __call__(self, path):
        self.calls += 1
        self.entered.set()
        self.release.wait(timeout=30)
        return self.value


class DrillTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.prod_storage = Storage(os.path.join(self.tmpdir, "prod.db"))
        self.prod_storage.set_setting("check_time", "07:15")
        self.clock = MutableClock(datetime(2026, 6, 15, 12, 0))
        self.reader = BlockingReader(auto_release=True)

        def factory(storage, clock):
            return Scheduler(
                storage=storage,
                volume_path="/volume",
                disk_reader=self.reader,
                poll_interval=0.01,
                clock=clock,
            )

        self.mgr = DrillManager(
            production_storage=self.prod_storage,
            scheduler_factory=factory,
            real_clock=self.clock,
            storage_factory=lambda: Storage(":memory:"),
        )

    def test_only_one_drill_at_a_time(self):
        first = self.mgr.start()
        with self.assertRaises(DrillAlreadyActiveError):
            self.mgr.start()
        self.assertIs(self.mgr.current(), first)
        self.mgr.exit()
        self.assertIsNone(self.mgr.current())
        # 退出后可再开新的一场，且仍是空的
        second = self.mgr.start()
        self.assertIsNot(second, first)
        self.assertEqual(second.storage.list_recent_results(), [])
        self.mgr.exit()

    def test_initial_check_time_comes_from_production_settings(self):
        session = self.mgr.start()
        self.assertEqual(session.storage.get_setting("check_time"), "07:15")
        self.assertEqual(session.check_time, "07:15")
        self.mgr.exit()

    def test_drill_changes_do_not_touch_production(self):
        session = self.mgr.start()
        session.scheduler.set_check_time("05:00")
        session.clock.advance(3600)
        session.scheduler.tick()  # 07:15 -> 11:15Z，已过点，读盘并落结果
        self.assertIsNotNone(session.storage.get_result("2026-06-15"))
        # 正式库：时刻没变，也没有计划/结果
        self.assertEqual(self.prod_storage.get_setting("check_time"), "07:15")
        self.assertIsNone(self.prod_storage.get_plan("2026-06-15"))
        self.assertIsNone(self.prod_storage.get_result("2026-06-15"))
        self.mgr.exit()

    def test_each_drill_starts_with_empty_records(self):
        session = self.mgr.start()
        session.clock.advance(2 * 3600)
        session.scheduler.tick()
        self.assertEqual(len(session.storage.list_recent_results()), 1)
        self.mgr.exit()
        second = self.mgr.start()
        self.assertEqual(second.storage.list_recent_results(), [])
        self.assertIsNone(second.storage.get_result("2026-06-15"))
        self.mgr.exit()

    def test_exit_while_reading_is_immediate_and_discards_late_result(self):
        self.reader = BlockingReader()  # 本用例需要真正阻塞读盘
        session = self.mgr.start()
        session.clock.advance(2 * 3600)  # 到点
        t = threading.Thread(target=session.scheduler.tick, daemon=True)
        t.start()
        self.assertTrue(self.reader.entered.wait(5), "读盘应已开始")

        began = time.monotonic()
        self.assertTrue(self.mgr.exit())
        self.assertLess(time.monotonic() - began, 1.5, "退出不应等待读盘结束")
        self.assertIsNone(self.mgr.current())

        # 读盘晚到：结果必须被丢弃
        self.reader.release.set()
        t.join(timeout=5)
        self.assertIsNone(session.storage.get_result("2026-06-15"))
        self.assertIsNone(session.scheduler._running_date)

    def test_drill_clock_offset_does_not_persist_to_production(self):
        session = self.mgr.start()
        session.clock.advance(7200)
        self.assertEqual(
            session.scheduler.now_aware_utc(),
            self.clock.now.replace(tzinfo=UTC) + timedelta(seconds=7200),
        )
        self.mgr.exit()
        self.assertIsNone(
            self.prod_storage.get_setting("clock_offset_seconds"),
            "演练时钟偏移不写正式库",
        )


class RestartModeTest(unittest.TestCase):
    def test_legacy_clock_offset_is_ignored_and_cleared_on_restart(self):
        tmpdir = tempfile.mkdtemp()
        db_path = os.path.join(tmpdir, "legacy.db")
        storage = Storage(db_path)
        storage.set_setting("clock_offset_seconds", "99999")
        storage.close()

        # 模拟重启：新进程打开旧库
        clock = MutableClock(datetime(2026, 6, 15, 12, 0))
        storage2 = Storage(db_path)
        sch = Scheduler(storage2, "/volume", clock=clock, read_delay=0)
        self.assertEqual(sch.now(), clock.now, "重启后忽略旧演练时钟偏移")
        self.assertIsNone(storage2.get_setting("clock_offset_seconds"))
        # 历史结果照常可查
        storage2.save_result("2026-06-14", "09:00", "2026-06-14T13:00:00+00:00",
                             None, "2026-06-14T13:00:01+00:00", 123, None)
        sch.tick()
        self.assertEqual(len(storage2.list_recent_results()), 1)


if __name__ == "__main__":
    unittest.main()
