"""Return/exchange record routes with Excel export."""

from __future__ import annotations

from io import BytesIO
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from openpyxl import Workbook
from pydantic import BaseModel


class ReturnRecordCreate(BaseModel):
    shop_id: int
    user_uid: str
    username: str = ""
    order_no: str
    order_status: str = "待核实"
    record_type: str
    new_address: str | None = None
    remark: str | None = None
    source_message: str | None = None
    conversation_id: int | None = None
    message_id: int | None = None


class ReturnRecordUpdate(BaseModel):
    shop_id: int | None = None
    user_uid: str | None = None
    username: str | None = None
    order_no: str | None = None
    order_status: str | None = None
    record_type: str | None = None
    new_address: str | None = None
    remark: str | None = None
    source_message: str | None = None
    conversation_id: int | None = None
    message_id: int | None = None


def build_router(ctx) -> APIRouter:
    router = APIRouter()

    @router.get("/api/return-records")
    def return_records(
        user: dict[str, Any] = Depends(ctx.current_user),
        shop_id: int | None = Query(default=None),
        record_type: str | None = Query(default=None),
        keyword: str | None = Query(default=None),
        date_from: str | None = Query(default=None),
        date_to: str | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ):
        where_sql, values = ctx.return_record_where(
            user,
            shop_id=shop_id,
            record_type=record_type,
            keyword=keyword,
            date_from=date_from,
            date_to=date_to,
        )
        total = ctx.repos.return_records.count(where_sql, values)
        rows = ctx.repos.return_records.search(where_sql, values, limit=limit, offset=offset)
        return {
            "items": [ctx.serialize_return_record(row) for row in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    @router.post("/api/return-records")
    def create_return_record(body: ReturnRecordCreate, user: dict[str, Any] = Depends(ctx.current_user)):
        data = ctx.validate_return_record_payload(body.model_dump(), user=user)
        row = ctx.upsert_return_record(data)
        result = ctx.serialize_return_record(row)
        ctx.hub.publish({"type": "return_record", "data": result})
        return result

    @router.patch("/api/return-records/{record_id}")
    def update_return_record(record_id: int, body: ReturnRecordUpdate, user: dict[str, Any] = Depends(ctx.current_user)):
        current = ctx.return_record_by_id(record_id, user)
        updates = body.dict(exclude_unset=True)
        if "shop_id" in updates and updates["shop_id"] is not None:
            ctx.can_access_shop(user, int(updates["shop_id"]))
        merged = {
            "shop_id": current.get("shop_id"),
            "conversation_id": current.get("conversation_id"),
            "message_id": current.get("message_id"),
            "user_uid": current.get("user_uid"),
            "username": current.get("username"),
            "order_no": current.get("order_no"),
            "order_status": current.get("order_status"),
            "record_type": current.get("record_type"),
            "new_address": current.get("new_address"),
            "remark": current.get("remark"),
            "source_message": current.get("source_message"),
            **updates,
        }
        data = ctx.validate_return_record_payload(merged, user=user)
        row = ctx.upsert_return_record(data, existing_id=record_id)
        result = ctx.serialize_return_record(row)
        ctx.hub.publish({"type": "return_record", "data": result})
        return result

    @router.delete("/api/return-records/{record_id}")
    def delete_return_record(record_id: int, user: dict[str, Any] = Depends(ctx.current_user)):
        row = ctx.return_record_by_id(record_id, user)
        ctx.repos.return_records.delete(record_id)
        ctx.hub.publish({"type": "return_record_deleted", "data": {"id": record_id, "shop_id": row.get("shop_id")}})
        return {"ok": True}

    @router.get("/api/return-records/export")
    def export_return_records(
        user: dict[str, Any] = Depends(ctx.current_user),
        shop_id: int | None = Query(default=None),
        record_type: str | None = Query(default=None),
        keyword: str | None = Query(default=None),
        date_from: str | None = Query(default=None),
        date_to: str | None = Query(default=None),
    ):
        where_sql, values = ctx.return_record_where(
            user,
            shop_id=shop_id,
            record_type=record_type,
            keyword=keyword,
            date_from=date_from,
            date_to=date_to,
        )
        rows = ctx.repos.return_records.search(where_sql, values)
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "退换记录"
        headers = ["ID", "店铺", "类型", "用户名", "用户UID", "订单号", "订单状态", "新地址", "备注", "来源消息", "创建时间"]
        sheet.append(headers)
        labels = {
            "return": "退货",
            "exchange": "换货",
            "address_change": "修改地址",
            "refund_only": "仅退款",
            "logistics_intercept": "物流拦截",
            "other": "其他",
        }
        for row in rows:
            sheet.append([
                row.get("id"),
                row.get("shop_name") or row.get("shop_id"),
                labels.get(str(row.get("record_type") or ""), row.get("record_type")),
                row.get("username"),
                row.get("user_uid"),
                row.get("order_no"),
                row.get("order_status"),
                row.get("new_address"),
                row.get("remark"),
                row.get("source_message"),
                row.get("created_at"),
            ])
        for column in sheet.columns:
            letter = column[0].column_letter
            sheet.column_dimensions[letter].width = min(36, max(10, max(len(str(cell.value or "")) for cell in column) + 2))
        stream = BytesIO()
        workbook.save(stream)
        stream.seek(0)
        headers = {"Content-Disposition": "attachment; filename=return-records.xlsx"}
        return StreamingResponse(
            stream,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers=headers,
        )

    @router.get("/api/return-records/{record_id}")
    def get_return_record(record_id: int, user: dict[str, Any] = Depends(ctx.current_user)):
        return ctx.serialize_return_record(ctx.return_record_by_id(record_id, user))

    return router
