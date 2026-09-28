from __future__ import annotations

import json
import re
from typing import Any

from openkefu.platforms.pdd.common import DEFAULT_USER_AGENT


GOODS_DETAIL_URL = "https://mms.pinduoduo.com/glide/v2/mms/query/commit/on_shop/detail"
GOODS_REFERER = "https://mms.pinduoduo.com/goods/goods_list"


class GoodsService:

    def __init__(self, login):
        self._login = login
        self._session = login.session
        self._http_lock = login._http_lock if hasattr(login, "_http_lock") else None

    def _headers(self, referer: str | None = None) -> dict[str, str]:
        headers = {
            "accept": "*/*",
            "accept-language": "zh-CN,zh;q=0.9",
            "cache-control": "max-age=0",
            "content-type": "application/json",
            "origin": "https://mms.pinduoduo.com",
            "priority": "u=1, i",
            "referer": referer or GOODS_REFERER,
            "sec-ch-ua": '"Chromium";v="148", "Microsoft Edge";v="148", "Not/A)Brand";v="99"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "user-agent": DEFAULT_USER_AGENT,
            "anti-content": self._login.get_latest_anti_content(referer or GOODS_REFERER),
        }
        return headers

    def get_product_detail(self, goods_id: int) -> dict[str, Any]:
        response = self._session.post(
            GOODS_DETAIL_URL,
            cookies=self._login._known_cookies(),
            headers=self._headers(),
            json={"goods_id": goods_id},
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()
        if not data.get("success"):
            raise RuntimeError(f"goods query failed: {data.get('error_msg') or 'unknown error'}")
        return data.get("result") or {}

    def format_product_for_llm(self, product: dict[str, Any]) -> str:
        if not product:
            return "商品信息获取失败"

        parts: list[str] = []

        name = product.get("goods_name") or ""
        if name:
            parts.append(f"商品名称：{name}")

        desc = product.get("goods_desc") or product.get("share_desc") or ""
        if desc and desc != name:
            parts.append(f"商品描述：{desc}")

        cat_path = [c for c in (product.get("cats") or []) if c]
        if cat_path:
            parts.append(f"商品类目：{' > '.join(cat_path)}")

        min_price = None
        max_price = None
        skus = product.get("skus") or []
        for sku in skus:
            p = sku.get("multi_price") or sku.get("price")
            if p:
                price_yuan = p / 100
                if min_price is None or price_yuan < min_price:
                    min_price = price_yuan
                if max_price is None or price_yuan > max_price:
                    max_price = price_yuan

        if min_price is not None and max_price is not None:
            if min_price == max_price:
                parts.append(f"价格：¥{min_price:.2f}")
            else:
                parts.append(f"价格区间：¥{min_price:.2f} ~ ¥{max_price:.2f}")

        if skus:
            spec_lines: list[str] = []
            for sku in skus:
                specs = sku.get("spec") or []
                spec_text = " / ".join(s.get("spec_name", "") for s in specs if s.get("spec_name"))
                price = sku.get("multi_price") or sku.get("price") or 0
                qty = sku.get("quantity") or 0
                line = f"  {spec_text}  ¥{price / 100:.2f}  库存{qty}"
                spec_lines.append(line)
            if spec_lines:
                parts.append("SKU 明细：\n" + "\n".join(spec_lines[:20]))
                if len(spec_lines) > 20:
                    parts.append(f"  ... 共 {len(spec_lines)} 个 SKU")

        goods_sn = product.get("goods_sn") or ""
        if goods_sn:
            parts.append(f"商品编码：{goods_sn}")

        status = product.get("status")
        status_label = {0: "下架", 1: "在售", 2: "审核中"}.get(status, f"状态{status}")
        parts.append(f"上架状态：{status_label}")

        mall_name = product.get("mall_name") or ""
        if mall_name:
            parts.append(f"所属店铺：{mall_name}")

        return "\n".join(parts)

    @staticmethod
    def extract_goods_ids(text: str) -> list[int]:
        ids: list[int] = []
        for m in re.finditer(r"goods_id[=/](\d+)", text):
            ids.append(int(m.group(1)))
        return list(dict.fromkeys(ids))
