"use strict";

const crypto = require("crypto");
const {buildAntiContentAsync} = require("./anti_content");
const {
  BASE_URL,
  buildCookies,
  buildFingerprint,
} = require("./pdd_context");

const CHAT_REFERER = `${BASE_URL}/chat-merchant/index.html`;
const DEFAULT_USER_AGENT =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
  "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36";

function generateSecWebSocketKey() {
  return crypto.randomBytes(16).toString("base64");
}

function parseCookieString(cookieString) {
  const cookies = {};
  for (const part of String(cookieString || "").split(";")) {
    const index = part.indexOf("=");
    if (index <= 0) {
      continue;
    }
    const name = part.slice(0, index).trim();
    const value = part.slice(index + 1).trim();
    if (name) {
      cookies[name] = value;
    }
  }
  return cookies;
}

function cookieHeader(cookies) {
  return Object.entries(cookies || {})
    .filter(([, value]) => value !== undefined && value !== null && value !== "")
    .map(([name, value]) => `${name}=${value}`)
    .join("; ");
}

function normalizeInput(input) {
  const options = input || {};
  const cookies = buildCookies({
    cookies: {
      ...parseCookieString(options.cookie || options.cookieString),
      ...(options.cookies || {}),
    },
    riskControlFp: options.riskControlFp,
  });
  return {
    ...options,
    cookies,
  };
}

async function buildGetTokenRequest(input) {
  const options = normalizeInput(input);
  const url = `${BASE_URL}/chats/getToken`;
  const fingerprint = buildFingerprint({
    locationHref: CHAT_REFERER,
    ...(options.fingerprintEnv || {}),
  });
  const antiContent = options.antiContent || await buildAntiContentAsync({
    fingerprint,
    cookies: options.cookies,
  });
  const headers = {
    accept: "*/*",
    "accept-language": "zh-CN,zh;q=0.9",
    "anti-content": antiContent,
    origin: BASE_URL,
    priority: "u=1, i",
    referer: CHAT_REFERER,
    "sec-ch-ua": "\"Google Chrome\";v=\"147\", \"Not.A/Brand\";v=\"8\", \"Chromium\";v=\"147\"",
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": "\"Windows\"",
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
    "user-agent": DEFAULT_USER_AGENT,
    cookie: cookieHeader(options.cookies),
  };

  const verifyAuthToken = options.verifyAuthToken || options.verifyauthtoken;
  if (verifyAuthToken) {
    headers.verifyauthtoken = verifyAuthToken;
  }

  return {
    method: "POST",
    url,
    headers,
    body: {
      version: String(options.version || 3),
    },
    cookies: options.cookies,
    antiContent,
  };
}

function buildWebSocketUrl(accessToken, input) {
  const options = input || {};
  const baseUrl = options.wsBaseUrl || "wss://m-ws.pinduoduo.com/";
  const serviceBuildTime = options.serviceBuildTime || 202604301140;
  const url = new URL(baseUrl);
  url.searchParams.set("access_token", accessToken);
  url.searchParams.set("role", options.role || "mall_cs");
  url.searchParams.set("client", options.client || "web");
  url.searchParams.set("version", String(serviceBuildTime || 0));
  return url.toString();
}

async function getChatAccessToken(input) {
  const request = await buildGetTokenRequest(input);
  const form = new FormData();
  form.append("version", request.body.version);

  const response = await fetch(request.url, {
    method: request.method,
    headers: request.headers,
    body: form,
    redirect: "manual",
  });
  const text = await response.text();
  let data;
  try {
    data = JSON.parse(text);
  } catch (err) {
    data = {text};
  }
  if (!response.ok) {
    const message = `getToken failed: HTTP ${response.status}`;
    throw Object.assign(new Error(message), {status: response.status, response: data});
  }
  const token = data && data.token;
  if (!token) {
    throw Object.assign(new Error("getToken response does not contain token"), {response: data});
  }
  return {
    token,
    accessToken: token,
    secWebSocketKey: generateSecWebSocketKey(),
    webSocketUrl: buildWebSocketUrl(token, input),
    antiContent: request.antiContent,
    response: data,
  };
}

async function readStdinJson() {
  let data = "";
  process.stdin.setEncoding("utf8");
  for await (const chunk of process.stdin) {
    data += chunk;
  }
  return data.trim() ? JSON.parse(data) : {};
}

async function main() {
  const requestedAction = process.argv[2];
  const input = await readStdinJson();
  const action = requestedAction || (
    input.cookies || input.cookie || input.cookieString ? "token" : "key"
  );
  if (action === "key" || action === "sec-key") {
    process.stdout.write(JSON.stringify({secWebSocketKey: generateSecWebSocketKey()}));
    return;
  }
  if (action === "build-request" || action === "request") {
    process.stdout.write(JSON.stringify(await buildGetTokenRequest(input)));
    return;
  }
  if (action === "url") {
    const token = input.token || input.accessToken;
    if (!token) {
      throw new Error("token is required for url action");
    }
    process.stdout.write(JSON.stringify({
      secWebSocketKey: generateSecWebSocketKey(),
      webSocketUrl: buildWebSocketUrl(token, input),
    }));
    return;
  }
  if (action !== "token") {
    throw new Error(`unknown action: ${action}`);
  }
  process.stdout.write(JSON.stringify(await getChatAccessToken(input)));
}

if (require.main === module) {
  main().catch((err) => {
    const payload = {
      error: err && err.message ? err.message : String(err),
      status: err && err.status,
      response: err && err.response,
    };
    process.stderr.write(JSON.stringify(payload));
    process.exit(1);
  });
}

module.exports = {
  generateSecWebSocketKey,
  buildGetTokenRequest,
  buildWebSocketUrl,
  getChatAccessToken,
};
