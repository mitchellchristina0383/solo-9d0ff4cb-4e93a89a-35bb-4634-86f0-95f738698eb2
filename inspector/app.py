"""Flask 巡检台应用工厂与路由。

正式巡检始终按真实纽约时间运行；演练是唯一的、与正式完全隔离的一场：
- 演练使用独立的内存数据库与独立调度线程，设置、计划、结果都不进正式库；
- 演练时钟偏移只存在于本场内存，重启即回到正式模式；
- 退出演练立即生效，正在读盘的晚到结果由演练调度器丢弃。
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import Optional

from flask import Flask, flash, redirect, render_template, request, url_for

from . import timeutil
from .drill import DrillAlreadyActiveError, DrillManager, DrillSession
from .scheduler import Scheduler, default_disk_reader, real_utc_now
from .storage import Storage

DST_LABELS = {
    "normal": "正常时刻",
    "gap_deferred": "春季跳时：已顺延到跳时后第一个有效分钟",
    "ambiguous_first": "秋季重复时刻：取第一次出现",
}

STATUS_LABELS = {
    "pending": "待执行",
    "running": "执行中",
    "done": "已完成",
    "error": "读盘失败",
}


def _plan_view(plan: Optional[dict]) -> Optional[dict]:
    if not plan:
        return None
    planned_ny = timeutil.from_iso(plan["planned_utc"]).astimezone(timeutil.NY_TZ)
    return {
        **plan,
        "planned_local": planned_ny.strftime("%Y-%m-%d %H:%M:%S %Z%z"),
        "dst_label": DST_LABELS.get(plan["dst_note"], plan["dst_note"]),
        "status_label": STATUS_LABELS.get(plan["status"], plan["status"]),
    }


def _result_view(result: Optional[dict]) -> Optional[dict]:
    if not result:
        return None
    r = dict(result)
    r["finished_local"] = timeutil.from_iso(
        r["finished_utc"]
    ).astimezone(timeutil.NY_TZ).strftime("%Y-%m-%d %H:%M:%S %Z%z")
    r["planned_local"] = timeutil.from_iso(
        r["planned_utc"]
    ).astimezone(timeutil.NY_TZ).strftime("%Y-%m-%d %H:%M:%S %Z%z")
    return r


def _status_views(status: dict) -> dict:
    return {
        "status": status,
        "plan": _plan_view(status["plan"]),
        "result": _result_view(status["result"]),
    }


def _build_drill_manager(production: Scheduler) -> DrillManager:
    def scheduler_factory(storage: Storage, clock) -> Scheduler:
        return Scheduler(
            storage=storage,
            volume_path=production.volume_path,
            disk_reader=production.disk_reader,
            read_delay=production.read_delay,
            poll_interval=production.poll_interval,
            clock=clock,
        )

    return DrillManager(
        production_storage=production.storage,
        scheduler_factory=scheduler_factory,
        real_clock=production.real_now,
    )


def create_app(db_path: str | None = None,
               scheduler: Scheduler | None = None,
               drill_manager: DrillManager | None = None) -> Flask:
    app = Flask(__name__)
    app.secret_key = os.environ.get("SECRET_KEY", "dev-inspector-secret")

    if scheduler is None:
        db_path = db_path or os.environ.get("DB_PATH", "/data/inspector.db")
        storage = Storage(db_path)
        volume = os.environ.get("VOLUME_PATH", "/volume")
        read_delay = float(os.environ.get("READ_DELAY_SECONDS", "0"))
        poll = float(os.environ.get("POLL_INTERVAL_SECONDS", "1"))
        scheduler = Scheduler(
            storage=storage,
            volume_path=volume,
            disk_reader=default_disk_reader,
            read_delay=read_delay,
            poll_interval=poll,
            clock=real_utc_now,
        )
    if drill_manager is None:
        drill_manager = _build_drill_manager(scheduler)

    app.scheduler = scheduler  # type: ignore[attr-defined]
    app.drill_manager = drill_manager  # type: ignore[attr-defined]

    def _active_drill() -> Optional[DrillSession]:
        return drill_manager.current()

    def _trigger_production():
        scheduler.wake()

    def _trigger_drill(session: DrillSession):
        session.scheduler.wake()

    def _require_drill() -> Optional[DrillSession]:
        session = _active_drill()
        if session is None:
            flash("当前没有进行中的演练，请先开始演练", "error")
        return session

    # ---- 正式巡检 -------------------------------------------------------
    @app.post("/check-time")
    def update_check_time():
        hhmm = request.form.get("check_time", "").strip()
        try:
            scheduler.set_check_time(hhmm)
        except timeutil.InvalidTimeError as exc:
            flash(f"时刻无效：{exc}", "error")
        else:
            flash(f"正式巡检时刻已更新为 {hhmm}", "ok")
            _trigger_production()
        return redirect(url_for("index"))

    # ---- 演练生命周期 ---------------------------------------------------
    @app.post("/drill/start")
    def drill_start():
        try:
            drill_manager.start()
        except DrillAlreadyActiveError as exc:
            flash(str(exc), "error")
        else:
            flash("演练已开始：本场使用空的演练记录，巡检时刻取自正式设置", "ok")
        return redirect(url_for("index"))

    @app.post("/drill/exit")
    def drill_exit():
        if drill_manager.exit():
            flash("演练已退出，页面回到正式巡检；晚到的演练结果已丢弃", "ok")
        else:
            flash("当前没有进行中的演练", "error")
        return redirect(url_for("index"))

    # ---- 演练设置 -------------------------------------------------------
    @app.post("/drill/check-time")
    def drill_update_check_time():
        session = _require_drill()
        if session is None:
            return redirect(url_for("index"))
        hhmm = request.form.get("check_time", "").strip()
        try:
            session.scheduler.set_check_time(hhmm)
        except timeutil.InvalidTimeError as exc:
            flash(f"演练时刻无效：{exc}", "error")
        else:
            flash(f"本场演练时刻已更新为 {hhmm}（不影响正式巡检）", "ok")
            _trigger_drill(session)
        return redirect(url_for("index"))

    @app.post("/drill/clock/set")
    @app.post("/clock/set")
    def clock_set():
        session = _require_drill()
        if session is None:
            return redirect(url_for("index"))
        value = request.form.get("utc_time", "").strip()
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            flash("演练时钟格式无效，请用 ISO 格式，如 2026-03-08T13:00", "error")
            return redirect(url_for("index"))
        session.clock.set_to(dt)
        flash("本场演练时钟已设定", "ok")
        _trigger_drill(session)
        return redirect(url_for("index"))

    @app.post("/clock/advance")
    @app.post("/drill/clock/advance")
    def clock_advance():
        session = _require_drill()
        if session is None:
            return redirect(url_for("index"))
        raw = request.form.get("seconds", "").strip()
        try:
            seconds = float(raw)
        except ValueError:
            flash("推进秒数无效", "error")
            return redirect(url_for("index"))
        session.clock.advance(seconds)
        flash(f"本场演练时钟已推进 {seconds:g} 秒", "ok")
        _trigger_drill(session)
        return redirect(url_for("index"))

    @app.post("/clock/reset")
    @app.post("/drill/clock/reset")
    def clock_reset():
        session = _require_drill()
        if session is None:
            return redirect(url_for("index"))
        session.clock.reset()
        flash("本场演练时钟已恢复为真实时间", "ok")
        _trigger_drill(session)
        return redirect(url_for("index"))

    @app.get("/")
    def index():
        production = scheduler.status()
        drill = _active_drill()
        drill_view = None
        if drill is not None:
            drill_status = drill.status()
            drill_view = {
                **_status_views(drill_status),
                "clock_offset_seconds": drill_status["clock_offset_seconds"],
                "started_real_local": drill.started_real_utc.replace(
                    tzinfo=timeutil.UTC
                ).astimezone(timeutil.NY_TZ).strftime("%Y-%m-%d %H:%M:%S %Z%z"),
            }
        return render_template(
            "index.html",
            production=_status_views(production),
            drill=drill_view,
        )

    return app
