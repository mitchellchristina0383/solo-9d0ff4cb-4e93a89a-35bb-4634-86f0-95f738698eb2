import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from inspector.app import create_app
from inspector.scheduler import Scheduler
from inspector.storage import Storage

UTC = timezone.utc


class WebTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.storage = Storage(os.path.join(self.tmpdir, "web.db"))
        self.clock_now = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)

        def clock():
            return self.clock_now.replace(tzinfo=None)

        self.sch = Scheduler(
            self.storage, "/volume",
            disk_reader=lambda p: 555,
            clock=clock,
        )
        self.app = create_app(scheduler=self.sch)
        self.client = self.app.test_client()

    def _start_drill(self):
        return self.client.post("/drill/start", follow_redirects=True)

    def _drill(self):
        return self.app.drill_manager.current()

    # ---- 正式巡检 -------------------------------------------------------
    def test_index_shows_production_plan(self):
        self.sch.tick()  # 生成当天计划
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn("2026-06-15", body)
        self.assertIn("09:00", body)
        self.assertIn("待执行", body)
        self.assertIn("开始演练", body)

    def test_set_check_time_via_form(self):
        resp = self.client.post("/check-time",
                                data={"check_time": "08:30"},
                                follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.storage.get_setting("check_time"), "08:30")
        plan = self.storage.get_plan("2026-06-15")
        self.assertEqual(plan["check_time"], "08:30")

    def test_invalid_check_time_flashes_error(self):
        resp = self.client.post("/check-time",
                                data={"check_time": "99:99"},
                                follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("时刻无效", resp.get_data(as_text=True))
        self.assertEqual(self.storage.get_setting("check_time"), "09:00")

    def test_read_error_displayed_on_page(self):
        def failing_reader(path):
            raise PermissionError("volume not mounted")

        self.sch.disk_reader = failing_reader
        self.sch.tick()  # 12:00Z 未到点，先建计划
        self.sch._clock = lambda: datetime(2026, 6, 15, 13, 0)
        self.sch.tick()
        resp = self.client.get("/")
        body = resp.get_data(as_text=True)
        self.assertIn("读盘失败", body)
        self.assertIn("PermissionError", body)
        self.assertIn("volume not mounted", body)

    # ---- 演练生命周期 ---------------------------------------------------
    def test_start_and_exit_drill_from_web(self):
        resp = self._start_drill()
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn("演练进行中", body)
        self.assertIn("退出演练", body)
        self.assertIsNotNone(self._drill())

        resp = self.client.post("/drill/exit", follow_redirects=True)
        body = resp.get_data(as_text=True)
        self.assertIn("演练已退出", body)
        self.assertIn("开始演练", body)
        self.assertIsNone(self._drill())

    def test_only_one_drill_at_a_time_from_web(self):
        self._start_drill()
        resp = self._start_drill()
        self.assertIn("已有演练在进行中", resp.get_data(as_text=True))
        self.assertEqual(self.app.drill_manager.current().storage.path, ":memory:")

    def test_drill_starts_with_empty_records_and_production_check_time(self):
        self.client.post("/check-time", data={"check_time": "23:00"})
        self._start_drill()
        drill = self._drill()
        self.assertEqual(drill.storage.get_setting("check_time"), "23:00")
        self.assertIsNone(drill.storage.get_result("2026-06-15"))
        self.assertEqual(drill.storage.list_recent_results(), [])

    def test_drill_clock_and_plan_are_isolated_from_production(self):
        self._start_drill()
        # 推进演练时钟到正式计划点，让演练完成一次读盘
        self.client.post("/drill/clock/advance", data={"seconds": "3600"})
        self._drill().scheduler.tick()
        self.assertIsNotNone(self._drill().storage.get_result("2026-06-15"))

        # 正式库完全不受影响
        self.assertIsNone(self.storage.get_result("2026-06-15"))
        self.assertEqual(self.storage.get_setting("check_time"), "09:00")

        resp = self.client.get("/")
        body = resp.get_data(as_text=True)
        self.assertIn("555", body)

    def test_drill_check_time_does_not_change_production(self):
        self._start_drill()
        self.client.post("/drill/check-time", data={"check_time": "05:30"})
        self.assertEqual(self._drill().storage.get_setting("check_time"), "05:30")
        self.assertEqual(self.storage.get_setting("check_time"), "09:00")

    def test_clock_routes_require_active_drill(self):
        resp = self.client.post("/drill/clock/advance",
                                data={"seconds": "60"}, follow_redirects=True)
        self.assertIn("当前没有进行中的演练", resp.get_data(as_text=True))
        resp = self.client.post("/drill/clock/set",
                                data={"utc_time": "2026-06-15T20:00"},
                                follow_redirects=True)
        self.assertIn("当前没有进行中的演练", resp.get_data(as_text=True))

    def test_drill_clock_set_and_reset(self):
        self._start_drill()
        self.client.post("/drill/clock/set",
                         data={"utc_time": "2026-06-15T20:00"})
        self.assertEqual(
            self._drill().scheduler.now_aware_utc(),
            datetime(2026, 6, 15, 20, 0, tzinfo=UTC),
        )
        self.client.post("/drill/clock/reset")
        self.assertEqual(
            self._drill().scheduler.now_aware_utc(),
            self.clock_now,
        )

    def test_production_keeps_running_while_drill_active(self):
        # 正式巡检到点照常执行，与演练并行
        self._start_drill()
        self.client.post("/drill/check-time", data={"check_time": "23:00"})
        self.sch._clock = lambda: datetime(2026, 6, 15, 13, 0)
        self.sch.tick()
        self.assertIsNotNone(self.storage.get_result("2026-06-15"))
        self.assertIsNone(self._drill().storage.get_result("2026-06-15"))


if __name__ == "__main__":
    unittest.main()
