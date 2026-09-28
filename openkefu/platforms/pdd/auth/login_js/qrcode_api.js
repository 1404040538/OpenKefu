"use strict";

const {buildAntiContentAsync} = require("./anti_content");
const {
  BASE_URL,
  XG_BASE_URL,
  buildCookies,
  buildFingerprint,
  buildHeaders,
  buildPfbHeaders,
  resolveRiskControlFp,
  shouldAddAntiHeader,
  randomizeFingerprint,
} = require("./pdd_context");
const {
  STATUS,
  parseQrcodeResponse,
  parseQueryResponse,
} = require("./response_parsers");

function buildQrcodeRequest(options) {
  const cookies = buildCookies(options || {});
  const url = `${BASE_URL}/janus/api/scan/login/qrcode`;
  return {
    method: "POST",
    url,
    headers: buildHeaders({...options, cookies}, url),
    cookies,
    body: {
      fingerprint: buildFingerprint((options || {}).fingerprintEnv),
    },
  };
}

function buildQueryRequest(options) {
  const input = options || {};
  if (!input.token) {
    throw new Error("buildQueryRequest: token is required");
  }
  const cookies = buildCookies(input);
  const url = `${BASE_URL}/janus/api/scan/login/query`;
  return {
    method: "POST",
    url,
    headers: buildHeaders({...input, cookies}, url),
    cookies,
    body: {
      data: input.token,
      fingerprint: buildFingerprint(input.fingerprintEnv),
    },
  };
}

function buildPfbTemplateContext(options) {
  const input = options || {};
  const cookies = buildCookies(input);
  const fingerprint = buildFingerprint(input.fingerprintEnv);
  const timestamp = input.timestamp || Date.now();
  const updates = {
    reportTimestamp: String(timestamp),
    uuid1: cookies.ru1k || cookies._f77 || "",
    uuid2: cookies.ru2k || cookies._a42 || "",
    cookie: resolveRiskControlFp({...input, cookies}) || "",
  };

  if (typeof input.uid !== "undefined") {
    updates.uid = String(input.uid);
  }

  // rawData 会嵌入加密后的 pfb 载荷，因此需要与 Anti-Content
  // 生成时使用的浏览器画像保持一致。
  const rawDataUpdates = {
    colorDepth: fingerprint.colorDepth,
    screenResolution: [fingerprint.width, fingerprint.height],
    availableScreenResolution: [fingerprint.availWidth, fingerprint.availHeight],
    cookiesEnabled: Boolean(fingerprint.navigator.cookieEnabled),
    vendor: fingerprint.navigator.vendor,
    productSub: fingerprint.navigator.productSub,
    ua: fingerprint.navigator.ua,
    language: fingerprint.navigator.language,
    hardwareConcurrency: fingerprint.navigator.hardwareConcurrency,
    platform: fingerprint.navigator.platform,
    timezoneOffset: fingerprint.timezoneOffset,
  };

  return {
    method: "POST",
    url: `${XG_BASE_URL}/xg/pfb/a2`,
    headers: buildPfbHeaders(input),
    cookies,
    timestamp,
    updates,
    rawDataUpdates,
  };
}

function buildSubSystemAuthTokenRequest(options) {
  const input = options || {};
  const cookies = buildCookies(input);
  const url = `${BASE_URL}/janus/api/subSystem/getAuthToken`;
  return {
    method: "POST",
    url,
    headers: buildHeaders({...input, cookies}, url),
    cookies,
    body: {
      subSystemId: typeof input.subSystemId === "number" ? input.subSystemId : 23,
    },
  };
}

function parseSubSystemAuthTokenResponse(options) {
  const input = options || {};
  const cookies = buildCookies(input);
  const response = input.response || {};
  const result = response.result || {};
  const authToken = response.authToken || result.authToken || "";
  if (authToken) {
    cookies.windows_app_shop_token_23 = authToken;
  }
  return {
    authToken,
    cookies,
  };
}

function buildCommonMallInfoRequest(options) {
  const input = options || {};
  const cookies = buildCookies(input);
  const url = `${BASE_URL}/earth/api/mallInfo/commonMallInfo`;
  return {
    method: "GET",
    url,
    headers: buildHeaders({...input, cookies}, url),
    cookies,
  };
}

function extractTargetCookies(options) {
  const cookies = buildCookies(options || {});
  const names = [
    "JSESSIONID",
    "mms_b84d1838",
    "PASS_ID",
    "windows_app_shop_token_23",
  ];
  const output = {};
  for (const name of names) {
    if (cookies[name]) {
      output[name] = cookies[name];
    }
  }
  return output;
}

async function buildAntiContentPayload(payload) {
  return {
    antiContent: await buildAntiContentAsync({
      fingerprint: buildFingerprint(payload && payload.fingerprintEnv),
      cookies: payload && payload.cookies,
    }),
  };
}

async function main() {
  const action = process.argv[2];
  const input = await readStdinJson();
  const handlers = {
    "build-qrcode": buildQrcodeRequest,
    "parse-qrcode": parseQrcodeResponse,
    "build-query": buildQueryRequest,
    "parse-query": parseQueryResponse,
    "build-pfb-context": buildPfbTemplateContext,
    "build-subsystem-auth-token": buildSubSystemAuthTokenRequest,
    "parse-subsystem-auth-token": parseSubSystemAuthTokenResponse,
    "build-common-mall-info": buildCommonMallInfoRequest,
    "extract-target-cookies": extractTargetCookies,
    "anti-content": buildAntiContentPayload,
    "randomize-fingerprint": async () => randomizeFingerprint(),
    cookies: buildCookies,
    fingerprint: (payload) => buildFingerprint(payload && payload.fingerprintEnv),
  };
  if (!handlers[action]) {
    throw new Error(`unknown action: ${action}`);
  }
  process.stdout.write(JSON.stringify(await handlers[action](input)));
}

function readStdinJson() {
  return new Promise((resolve, reject) => {
    let data = "";
    process.stdin.setEncoding("utf8");
    process.stdin.on("data", (chunk) => {
      data += chunk;
    });
    process.stdin.on("end", () => {
      if (!data.trim()) {
        resolve({});
        return;
      }
      try {
        resolve(JSON.parse(data));
      } catch (err) {
        reject(err);
      }
    });
  });
}

if (require.main === module) {
  main().catch((err) => {
    process.stderr.write(err && err.stack ? err.stack : String(err));
    process.exit(1);
  });
}

module.exports = {
  STATUS,
  buildFingerprint,
  buildCookies,
  buildPfbTemplateContext,
  shouldAddAntiHeader,
  buildSubSystemAuthTokenRequest,
  parseSubSystemAuthTokenResponse,
  buildCommonMallInfoRequest,
  extractTargetCookies,
  buildQrcodeRequest,
  buildQueryRequest,
  parseQrcodeResponse,
  parseQueryResponse,
};
