"""Background live monitoring and operational notifications."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from ashare_ai.core.config import get_settings
from ashare_ai.monitoring.health import check_runtime_dependencies
from ashare_ai.monitoring.realtime import build_live_snapshot
from ashare_ai.notifications.service import NotificationService
from ashare_ai.portfolio.user_assets import UserAssetService
from ashare_ai.storage.database import SessionLocal
from ashare_ai.storage.models import NotificationRow, PushDeliveryRow, UserAccount, UserAssetState


def run_live_monitoring_job(_job_id: str) -> dict[str, Any]:
    now = datetime.now(UTC)
    checked = 0
    created = 0
    with SessionLocal() as session:
        # A row is required so a newly created account does not unexpectedly
        # start polling the demo defaults returned by UserAssetService.get().
        rows = list(session.scalars(select(UserAssetState)).all())
        users = {row.user_id: row for row in session.scalars(select(UserAccount).where(UserAccount.enabled.is_(True))).all()}
        notifications = NotificationService(session)
        for row in rows:
            if row.user_id not in users or not (row.positions or row.watchlist):
                continue
            snapshot = build_live_snapshot(row.user_id, now=now, include_watchlist=True)
            checked += 1
            active_symbols: set[str] = set()
            for signal in snapshot.get("signals", []):
                symbol = str(signal.get("symbol") or "")
                state = str(signal.get("signal_type") or "RISK")
                if not symbol or state == "NORMAL":
                    continue
                active_symbols.add(symbol)
                anomaly = str(signal.get("signal_type", "")).startswith(("INTRADAY_", "VOLUME_", "MA_"))
                if anomaly and str(signal.get("severity")) not in {"HIGH", "CRITICAL"}:
                    continue
                notice = notifications.create(
                    user_id=row.user_id,
                    notification_type="LIVE_MONITOR_ANOMALY" if anomaly else "LIVE_MONITOR_RISK",
                    severity=str(signal.get("severity") or "WARNING"),
                    title=f"{symbol} 状况提醒",
                    body=f"{state}，请打开监控查看最新证据。",
                    resource_type="SYMBOL",
                    resource_id=symbol,
                    payload={"signal": signal, "monitor_version": snapshot.get("monitor_version")},
                    dedupe_key=f"live-anomaly:{row.user_id}:{symbol}:{state}" if anomaly else f"live-risk:{row.user_id}:{symbol}:{state}",
                    now=now,
                )
                if notice.created_at == now:
                    created += 1
            # A NORMAL edge closes the most recent non-normal risk state. The
            # recovery notification has its own key, so it is emitted once per
            # transition and cannot spam on every monitor tick.
            position_symbols = {
                str(item.get("symbol") or "") for item in snapshot.get("positions", [])
            }
            for symbol in position_symbols - active_symbols:
                previous = session.scalar(
                    select(NotificationRow)
                    .where(
                        NotificationRow.user_id == row.user_id,
                        NotificationRow.notification_type == "LIVE_MONITOR_RISK",
                        NotificationRow.resource_id == symbol,
                    )
                    .order_by(NotificationRow.created_at.desc())
                    .limit(1)
                )
                payload = previous.payload if previous is not None and isinstance(previous.payload, dict) else {}
                previous_signal = payload.get("signal") if isinstance(payload, dict) else None
                previous_state = previous_signal.get("signal_type") if isinstance(previous_signal, dict) else None
                if previous_state and previous_state != "NORMAL":
                    notifications.create(
                        user_id=row.user_id,
                        notification_type="LIVE_MONITOR_RECOVERY",
                        severity="INFO",
                        title=f"{symbol} 状况恢复",
                        body="风险边沿已恢复正常，监控将继续跟踪。",
                        resource_type="SYMBOL",
                        resource_id=symbol,
                        payload={"recovered_from": previous_state, "monitor_version": snapshot.get("monitor_version")},
                        dedupe_key=f"live-recovery:{row.user_id}:{symbol}:{previous_state}",
                        now=now,
                    )
            if snapshot.get("stale"):
                notifications.create(
                    user_id=row.user_id,
                    notification_type="MARKET_DATA_STALE",
                    severity="WARNING",
                    title="行情数据延迟",
                    body="当前行情超过新鲜度阈值，监控结果已标记为延迟。",
                    payload={"generated_at": snapshot.get("generated_at")},
                    dedupe_key=f"market-stale:{row.user_id}",
                    now=now,
                )

        details = check_runtime_dependencies(get_settings(), include_external=False)
        degraded = [name for name in ("database", "redis") if details.get(name, {}).get("status") != "ok"]
        resources = details.get("resources") or {}
        if resources.get("level") in {"WARNING", "CRITICAL"}:
            degraded.append(f"resources:{resources.get('level')}")
        for component in degraded:
            notifications.create_for_administrators(
                notification_type="SYSTEM_HEALTH_DEGRADED",
                severity="CRITICAL" if component in {"database", "redis"} else "WARNING",
                title="系统状况需要关注",
                body=f"{component} 当前不可用或资源紧张。",
                payload={"component": component},
                dedupe_key=f"system-health:{component}",
                now=now,
            )
        failed_deliveries = list(
            session.scalars(
                select(PushDeliveryRow)
                .where(PushDeliveryRow.status == "FAILED")
                .order_by(PushDeliveryRow.updated_at.desc())
                .limit(100)
            ).all()
        )
        if failed_deliveries:
            notifications.create_for_administrators(
                notification_type="PUSH_DELIVERY_FAILED",
                severity="WARNING",
                title="推送投递失败",
                body=f"有 {len(failed_deliveries)} 条移动端推送达到重试上限。",
                payload={"failed_count": len(failed_deliveries)},
                dedupe_key="push-delivery-failed",
                now=now,
            )
        session.commit()
    return {"users_checked": checked, "notifications_created": created, "degraded": degraded}
