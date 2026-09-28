from __future__ import annotations

import re
from typing import Any


ORDERS_URL = "https://mms.pinduoduo.com/latitude/order/userAllOrder"
ORDERS_REFERER = "https://mms.pinduoduo.com/chat-merchant/index.html"


class OrderService:

    def __init__(self, login):
        self._login = login
        self._session = login.session

    def _headers(self, referer: str | None = None) -> dict[str, str]:
        return {
            "accept": "*/*",
            "accept-language": "zh-CN,zh;q=0.9",
            "content-type": "application/json",
            "origin": "https://mms.pinduoduo.com",
            "priority": "u=1, i",
            "referer": referer or ORDERS_REFERER,
            "sec-ch-ua": '"Chromium";v="148", "Microsoft Edge";v="148", "Not/A)Brand";v="99"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36 Edg/148.0.0.0",
            "anti-content": self._login.get_latest_anti_content(referer or ORDERS_REFERER),
        }

    def get_user_orders(self, uid: str, *, page_no: int = 1, page_size: int = 10) -> dict[str, Any]:
        response = self._session.post(
            ORDERS_URL,
            cookies=self._login._known_cookies(),
            headers=self._headers(),
            json={
                "pageNo": page_no,
                "pageSize": page_size,
                "showHistory": True,
                "uid": uid,
            },
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()
        if not data.get("success"):
            raise RuntimeError(f"order query failed: {data.get('error_msg') or 'unknown error'}")
        return data

    def format_orders_for_llm(self, data: dict[str, Any]) -> str:
        result = data.get("result") or {}
        if not isinstance(result, dict):
            return "订单数据获取失败"

        order_list = result.get("orderList") or result.get("orders") or result.get("list") or []
        if not isinstance(order_list, list):
            order_list = []

        total = result.get("total") or result.get("totalCount") or len(order_list)

        if not order_list:
            return f"该用户暂无订单记录（共 {total} 条）"

        parts: list[str] = [f"用户订单列表（共 {total} 条，当前第 {result.get('pageNo', 1)} 页）："]
        for idx, order in enumerate(order_list[:30], start=1):
            if not isinstance(order, dict):
                continue
            lines = [f"[{idx}]"]
            order_sn = order.get("order_sn") or order.get("orderSn") or ""
            if order_sn:
                lines.append(f"  订单号：{order_sn}")
            status = order.get("order_status") or order.get("orderStatus") or order.get("status") or ""
            status_label = _order_status_label(status)
            if status_label:
                lines.append(f"  状态：{status_label}")
            amount = order.get("amount") or order.get("order_amount") or order.get("pay_amount") or order.get("orderAmount") or 0
            if amount:
                lines.append(f"  金额：¥{float(amount) / 100:.2f}")
            goods_list = order.get("goods_list") or order.get("goodsList") or order.get("items") or []
            if isinstance(goods_list, list) and goods_list:
                goods_lines = []
                for g in goods_list:
                    gname = g.get("goods_name") or g.get("goodsName") or g.get("name") or ""
                    gcount = g.get("count") or g.get("quantity") or g.get("goods_number") or 0
                    gspec = g.get("spec") or g.get("goods_spec") or ""
                    text = f"    - {gname}"
                    if gspec:
                        text += f" [{gspec}]"
                    if gcount:
                        text += f" x{gcount}"
                    goods_lines.append(text)
                if goods_lines:
                    lines.append(f"  商品：\n" + "\n".join(goods_lines))
            if order_sn:
                pass
            created = order.get("created_at") or order.get("createdAt") or order.get("order_time") or ""
            if created:
                lines.append(f"  下单时间：{created}")
            after_sale = order.get("after_sale_status") or order.get("afterSaleStatus") or ""
            if after_sale:
                lines.append(f"  售后状态：{after_sale}")
            shipping = order.get("shipping_status") or order.get("shippingStatus") or order.get("tracking_status") or ""
            if shipping:
                lines.append(f"  物流：{shipping}")
            parts.append("\n".join(lines))
        return "\n\n".join(parts)

    @staticmethod
    def extract_uids(text: str) -> list[str]:
        ids: list[str] = []
        for m in re.finditer(r"(?:uid[=/]|用户I?[dD][=：:]\s*)(\S+)", text):
            ids.append(m.group(1).strip(",，;；"))
        for m in re.finditer(r"uid\s*:\s*(\S+)", text, re.IGNORECASE):
            ids.append(m.group(1).strip(",，;；"))
        return list(dict.fromkeys(ids))


def _order_status_label(status: str | int) -> str:
    if not isinstance(status, str):
        status = str(status)
    labels: dict[str, str] = {
        "0": "待付款",
        "1": "待发货",
        "2": "已发货",
        "3": "已签收",
        "4": "已取消",
        "5": "已完成",
        "6": "退款中",
        "7": "已退款",
        "unpaid": "待付款",
        "paid": "待发货",
        "shipped": "已发货",
        "completed": "已完成",
        "cancelled": "已取消",
        "refund": "退款中",
    }
    return labels.get(status.lower() if hasattr(status, "lower") else status, status)
