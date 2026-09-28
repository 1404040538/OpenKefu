"use strict";

const STATUS = {
  WAITING_SCAN: 1,
  WAITING_CONFIRM: 2,
  SUCCESS: 3,
  BUSY: 5,
};

function unwrapJanusResponse(response, options) {
  const allowBusinessError = options && options.allowBusinessError;
  if (response && Object.prototype.hasOwnProperty.call(response, "success")) {
    if (response.success) {
      return response.result;
    }
    if (allowBusinessError) {
      return {
        ok: false,
        errorCode: response.errorCode || response.error_code || null,
        errorMsg: response.errorMsg || response.error_msg || "",
        verifyAuthToken: response.result && response.result.verifyAuthToken,
        raw: response,
      };
    }
    const err = new Error(response.errorMsg || response.error_msg || "janus api error");
    err.response = response;
    throw err;
  }
  if (
    response &&
    Object.prototype.hasOwnProperty.call(response, "result") &&
    Object.prototype.hasOwnProperty.call(response, "data")
  ) {
    return response.data;
  }
  return response;
}

function parseUriQuery(uri) {
  if (!uri) {
    return {};
  }
  const query = uri.includes("?") ? uri.slice(uri.indexOf("?") + 1) : uri;
  const params = new URLSearchParams(query);
  const output = {};
  for (const [key, value] of params.entries()) {
    output[key] = value;
  }
  return output;
}

function parseQrcodeResponse(response) {
  const result = unwrapJanusResponse(response, {allowBusinessError: true}) || {};
  if (result.ok === false) {
    return {
      uri: "",
      token: "",
      ok: false,
      codeStatus: result.errorCode || 500000,
      errorCode: result.errorCode,
      errorMsg: result.errorMsg,
      verifyAuthToken: result.verifyAuthToken || "",
      raw: result.raw,
    };
  }
  const uri = result.uri || "";
  const data = parseUriQuery(uri).data || "";
  return {
    uri,
    token: data,
    ok: Boolean(uri && data),
    codeStatus: uri && data ? STATUS.WAITING_SCAN : 500000,
  };
}

function parseQueryResponse(response) {
  const result = unwrapJanusResponse(response, {allowBusinessError: true}) || {};
  if (result.ok === false) {
    return {
      status: null,
      ok: false,
      errorCode: result.errorCode,
      errorMsg: result.errorMsg,
      verifyAuthToken: result.verifyAuthToken || "",
      raw: result.raw,
      isPolling: false,
      isSuccess: false,
      isBusy: false,
    };
  }
  const data = result && typeof result.data === "object" && result.data !== null ? result.data : {};
  const query = result && typeof result.query === "object" && result.query !== null ? result.query : {};
  const status = result.status ?? data.status ?? query.status;
  const normalized = {
    ...result,
    ...data,
    ...query,
    status,
    token: result.token ?? data.token ?? query.token ?? null,
    windowsAppShopToken: result.windowsAppShopToken ?? data.windowsAppShopToken ?? query.windowsAppShopToken ?? null,
    verifyAuthToken: result.verifyAuthToken ?? data.verifyAuthToken ?? query.verifyAuthToken ?? "",
  };
  return {
    ...normalized,
    isPolling: status === STATUS.WAITING_SCAN || status === STATUS.WAITING_CONFIRM,
    isSuccess: status === STATUS.SUCCESS,
    isBusy: status === STATUS.BUSY,
  };
}

module.exports = {
  STATUS,
  parseQrcodeResponse,
  parseQueryResponse,
  unwrapJanusResponse,
};
