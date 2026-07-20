from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, case, or_, select
from sqlalchemy.orm import Session

from ashare_ai.agents.attachments import AttachmentService
from ashare_ai.storage.models import AIChatThread


class InvalidThreadCursor(ValueError):
    pass


def automatic_group(mentions: list[dict[str, str]]) -> tuple[str, str]:
    unique: dict[str, str] = {}
    for item in mentions:
        symbol = str(item.get("symbol") or "")
        name = str(item.get("name") or "").strip()
        if symbol:
            unique[symbol] = name or symbol
    if not unique:
        return "GENERAL", "综合问答"
    if len(unique) == 1:
        return "SINGLE", next(iter(unique.values()))
    return "MULTI", "多股票"


class ChatThreadService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def list_index(
        self,
        *,
        user_id: str,
        limit: int,
        cursor: str | None,
        archived: bool,
        query: str | None,
    ) -> tuple[list[AIChatThread], str | None]:
        pinned_rank = case((AIChatThread.pinned_at.is_not(None), 1), else_=0)
        statement = select(AIChatThread).where(
            AIChatThread.user_id == user_id,
            (
                AIChatThread.archived_at.is_not(None)
                if archived
                else AIChatThread.archived_at.is_(None)
            ),
        )
        normalized_query = (query or "").strip()
        if normalized_query:
            pattern = f"%{normalized_query}%"
            statement = statement.where(
                or_(AIChatThread.title.ilike(pattern), AIChatThread.group_label.ilike(pattern))
            )
        if cursor:
            rank, updated_at, thread_id = _decode_cursor(cursor)
            statement = statement.where(
                or_(
                    pinned_rank < rank,
                    and_(pinned_rank == rank, AIChatThread.updated_at < updated_at),
                    and_(
                        pinned_rank == rank,
                        AIChatThread.updated_at == updated_at,
                        AIChatThread.thread_id < thread_id,
                    ),
                )
            )
        rows = list(
            self.session.scalars(
                statement.order_by(
                    pinned_rank.desc(),
                    AIChatThread.updated_at.desc(),
                    AIChatThread.thread_id.desc(),
                ).limit(limit + 1)
            ).all()
        )
        if len(rows) <= limit:
            return rows, None
        rows = rows[:limit]
        last = rows[-1]
        return rows, _encode_cursor(last)

    def patch(self, user_id: str, thread_id: str, changes: dict[str, Any]) -> AIChatThread:
        row = self._owned(user_id, thread_id)
        if "title" in changes and changes["title"] is not None:
            row.title = str(changes["title"]).strip()
        if changes.get("pinned") is not None:
            row.pinned_at = datetime.now(UTC) if changes["pinned"] else None
        if changes.get("archived") is not None:
            row.archived_at = datetime.now(UTC) if changes["archived"] else None
        if "group_label" in changes:
            label = str(changes.get("group_label") or "").strip()
            if label:
                row.group_mode = "MANUAL"
                row.group_label = label
            else:
                row.group_mode = "AUTO"
                row.group_type, row.group_label = automatic_group(row.cumulative_mentions)
        row.updated_at = datetime.now(UTC)
        self.session.commit()
        return row

    def delete(self, user_id: str, thread_id: str) -> None:
        row = self._owned(user_id, thread_id)
        AttachmentService(self.session).purge_thread(user_id, thread_id)
        self.session.delete(row)
        self.session.commit()

    def bulk_delete(self, user_id: str, thread_ids: list[str]) -> int:
        unique_ids = list(dict.fromkeys(thread_ids))
        rows = list(
            self.session.scalars(
                select(AIChatThread).where(
                    AIChatThread.user_id == user_id,
                    AIChatThread.thread_id.in_(unique_ids),
                )
            ).all()
        )
        for row in rows:
            AttachmentService(self.session).purge_thread(user_id, row.thread_id)
            self.session.delete(row)
        self.session.commit()
        return len(rows)

    def _owned(self, user_id: str, thread_id: str) -> AIChatThread:
        row = self.session.scalar(
            select(AIChatThread).where(
                AIChatThread.user_id == user_id, AIChatThread.thread_id == thread_id
            )
        )
        if row is None:
            raise KeyError(thread_id)
        return row


def _encode_cursor(row: AIChatThread) -> str:
    updated_at = row.updated_at
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=UTC)
    payload = json.dumps(
        [1 if row.pinned_at is not None else 0, updated_at.isoformat(), row.thread_id],
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[int, datetime, str]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded).decode())
        rank = int(value[0])
        updated_at = datetime.fromisoformat(str(value[1]))
        thread_id = str(value[2])
        if rank not in {0, 1} or not thread_id:
            raise ValueError
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=UTC)
        return rank, updated_at, thread_id
    except (ValueError, TypeError, KeyError, IndexError, json.JSONDecodeError) as exc:
        raise InvalidThreadCursor("invalid thread cursor") from exc
