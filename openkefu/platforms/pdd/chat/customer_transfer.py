from __future__ import annotations

from typing import Any

from openkefu.platforms.pdd.chat.auto_reply import incoming_user_messages
from openkefu.platforms.pdd.common import json_or_text, json_request_data


DEFAULT_TRANSFER_REMARK = "\u65e0\u539f\u56e0\u76f4\u63a5\u8f6c\u79fb"


class CustomerTransferClient:
    ASSIGN_CS_LIST_PATH = "/latitude/assign/getAssignCsList"
    MOVE_CONVERSATION_PATH = "/plateau/chat/move_conversation"
    CLOSE_CONVERSATION_PATH = "/plateau/chat/close_conversation"

    def __init__(self, customer_service):
        self.customer_service = customer_service
        self.login = customer_service.login

    def get_assign_cs_list(
        self,
        *,
        wechat_check=True,
        referer=None,
        timeout=15,
    ):
        body = {
            "wechatCheck": bool(wechat_check),
        }
        response = self.customer_service._send(
            "POST",
            self.ASSIGN_CS_LIST_PATH,
            referer=referer,
            timeout=timeout,
            headers={
                "cache-control": "no-cache",
                "content-type": "application/json",
                "pragma": "no-cache",
            },
            data=json_request_data(body),
        )
        result = json_or_text(response)
        self.customer_service._raise_for_api_failure(result)
        return self.customer_service._unwrap_result(result)

    def list_customer_services(self, *, wechat_check=True, referer=None, timeout=15):
        result = self.get_assign_cs_list(
            wechat_check=wechat_check,
            referer=referer,
            timeout=timeout,
        )
        cs_list = (result or {}).get("csList") or {}
        services = []
        for csid, item in cs_list.items():
            item = dict(item or {})
            item.setdefault("csid", str(csid))
            services.append(item)
        return services

    def get_transfer_reasons(self, *, wechat_check=True, referer=None, timeout=15):
        result = self.get_assign_cs_list(
            wechat_check=wechat_check,
            referer=referer,
            timeout=timeout,
        )
        return list((result or {}).get("transReason") or [])

    def find_customer_service(
        self,
        value,
        *,
        wechat_check=True,
        referer=None,
        timeout=15,
    ):
        value = str(value)
        for item in self.list_customer_services(
            wechat_check=wechat_check,
            referer=referer,
            timeout=timeout,
        ):
            candidates = {
                str(item.get("csid") or ""),
                str(item.get("id") or ""),
                str(item.get("username") or ""),
                str(item.get("nickname") or ""),
            }
            if value in candidates:
                return item
        return None

    def build_move_conversation_body(
        self,
        *,
        csid,
        user_uid,
        need_wx=False,
        remark=DEFAULT_TRANSFER_REMARK,
        request_id=None,
        client="WEB",
        referer=None,
        inner_anti_content=None,
        outer_anti_content=None,
    ):
        if csid in (None, ""):
            raise ValueError("csid is required")
        if user_uid in (None, ""):
            raise ValueError("user_uid is required")

        referer = referer or self.customer_service.CHAT_REFERER
        inner_anti_content = inner_anti_content or self.login.get_latest_anti_content(referer)
        outer_anti_content = outer_anti_content or self.login.get_latest_anti_content(referer)
        command = {
            "cmd": "move_conversation",
            "request_id": request_id or self.customer_service._next_request_id(),
            "conversation": {
                "csid": str(csid),
                "uid": str(user_uid),
                "need_wx": bool(need_wx),
                "remark": "" if remark is None else str(remark),
            },
            "anti_content": inner_anti_content,
        }
        return {
            "data": command,
            "client": client,
            "anti_content": outer_anti_content,
        }

    def move_conversation(
        self,
        *,
        csid,
        user_uid,
        need_wx=False,
        remark=DEFAULT_TRANSFER_REMARK,
        request_id=None,
        client="WEB",
        referer=None,
        timeout=15,
    ):
        with self.customer_service._http_lock:
            body = self.build_move_conversation_body(
                csid=csid,
                user_uid=user_uid,
                need_wx=need_wx,
                remark=remark,
                request_id=request_id,
                client=client,
                referer=referer,
            )
            response = self.customer_service._send(
                "POST",
                self.MOVE_CONVERSATION_PATH,
                referer=referer,
                timeout=timeout,
                headers={
                    "cache-control": "no-cache",
                    "content-type": "application/json",
                    "pragma": "no-cache",
                },
                data=json_request_data(body),
            )
        result = json_or_text(response)
        self.customer_service._raise_for_api_failure(result)
        return self.customer_service._unwrap_result(result)

    def build_close_conversation_body(
        self,
        *,
        user_uid,
        request_id=None,
        client="WEB",
        referer=None,
        inner_anti_content=None,
        outer_anti_content=None,
    ):
        if user_uid in (None, ""):
            raise ValueError("user_uid is required")

        referer = referer or self.customer_service.CHAT_REFERER
        inner_anti_content = inner_anti_content or self.login.get_latest_anti_content(referer)
        outer_anti_content = outer_anti_content or self.login.get_latest_anti_content(referer)
        command = {
            "cmd": "close_conversation",
            "request_id": request_id or self.customer_service._next_request_id(),
            "uid": str(user_uid),
            "conversation": {
                "uid": str(user_uid),
            },
            "anti_content": inner_anti_content,
        }
        return {
            "data": command,
            "client": client,
            "anti_content": outer_anti_content,
        }

    def close_conversation(
        self,
        *,
        user_uid,
        request_id=None,
        client="WEB",
        referer=None,
        timeout=15,
    ):
        with self.customer_service._http_lock:
            body = self.build_close_conversation_body(
                user_uid=user_uid,
                request_id=request_id,
                client=client,
                referer=referer,
            )
            response = self.customer_service._send(
                "POST",
                self.CLOSE_CONVERSATION_PATH,
                referer=referer,
                timeout=timeout,
                headers={
                    "cache-control": "no-cache",
                    "content-type": "application/json",
                    "pragma": "no-cache",
                },
                data=json_request_data(body),
            )
        result = json_or_text(response)
        self.customer_service._raise_for_api_failure(result)
        return self.customer_service._unwrap_result(result)

    def move_conversation_to_service(
        self,
        customer_service_value,
        *,
        user_uid,
        need_wx=False,
        remark=DEFAULT_TRANSFER_REMARK,
        wechat_check=True,
        referer=None,
        timeout=15,
    ):
        service = self.find_customer_service(
            customer_service_value,
            wechat_check=wechat_check,
            referer=referer,
            timeout=timeout,
        )
        if not service:
            raise ValueError(f"customer service not found: {customer_service_value!r}")
        return self.move_conversation(
            csid=service["csid"],
            user_uid=user_uid,
            need_wx=need_wx,
            remark=remark,
            referer=referer,
            timeout=timeout,
        )

    def move_event_conversation(
        self,
        event: dict[str, Any],
        *,
        csid,
        need_wx=False,
        remark=DEFAULT_TRANSFER_REMARK,
        referer=None,
        timeout=15,
    ):
        user_uid = self.customer_service.extract_customer_uid(event)
        if not user_uid:
            raise ValueError(f"cannot find customer uid from event={event!r}")
        return self.move_conversation(
            csid=csid,
            user_uid=user_uid,
            need_wx=need_wx,
            remark=remark,
            referer=referer,
            timeout=timeout,
        )

    def move_event_conversations(
        self,
        event: dict[str, Any],
        *,
        csid,
        need_wx=False,
        remark=DEFAULT_TRANSFER_REMARK,
        referer=None,
        timeout=15,
    ):
        results = []
        for user_message in incoming_user_messages(event):
            user_uid = self.extract_user_uid(user_message)
            target_csid = csid(user_message) if callable(csid) else csid
            if target_csid in (None, ""):
                raise ValueError(f"cannot resolve csid from user_message={user_message!r}")
            if not user_uid:
                raise ValueError(f"cannot find customer uid from user_message={user_message!r}")
            results.append(
                self.move_conversation(
                    csid=target_csid,
                    user_uid=user_uid,
                    need_wx=need_wx,
                    remark=remark,
                    referer=referer,
                    timeout=timeout,
                )
            )
        return results

    @staticmethod
    def extract_user_uid(user_message):
        if not isinstance(user_message, dict):
            return ""
        sender = user_message.get("from") or {}
        if sender.get("role") == "user" and sender.get("uid") not in (None, ""):
            return str(sender.get("uid"))
        return ""
