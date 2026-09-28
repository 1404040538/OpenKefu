import json
import logging

from openkefu.platforms.pdd.auth.login import Login
from openkefu.platforms.pdd.chat.auto_reply import AutoReplyHandler
from openkefu.platforms.pdd.chat.customer_service import CustomerServiceClient


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)


def main():
    def on_titan_send(message):
        pass

    def on_titan_receive(message):
        auto_reply.handle(message)

    def on_titan_error(error):
        logger.error("titan listener error: %s", error, exc_info=error)

    login = Login()
    login_result = login.get_qrcode_and_wait_for_login()
    logger.info("login success qrcode_path=%s", login_result["qrcode_path"])

    customer_service = CustomerServiceClient(login)
    version = customer_service.get_version()
    logger.info("version=%s", version)

    token_result = customer_service.get_token()
    mall_id = token_result["mall_id"]
    access_token = token_result["token"]
    ws_base_url = token_result.get("use_ip")
    logger.info("token ready mall_id=%s has_access_token=%s use_ip=%s", mall_id, bool(access_token), ws_base_url)

    auto_reply = AutoReplyHandler(
        customer_service,
        token_result=token_result,
        mall_id=mall_id,
    )

    listener = customer_service.start_titan_listener(
        access_token=access_token,
        token_result=token_result,
        on_send=on_titan_send,
        on_receive=on_titan_receive,
        on_error=on_titan_error,
    )
    logger.info("titan listener started thread=%s", listener["thread"].name)


if __name__ == "__main__":
    main()
