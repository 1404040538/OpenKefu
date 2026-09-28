"use strict";

const { buildAntiContent } = require("./anti_content");
const {
  BASE_URL,
  buildCookies,
  buildFingerprint,
  buildHeaders,
  resolveRiskControlFp,
  randomizeFingerprint,
} = require("./pdd_context");

/**
 * Build the request to query password encryption config (public key).
 */
function buildQueryPasswordEncryptRequest(options) {
  const input = options || {};
  const cookies = buildCookies(input);
  const url = `${BASE_URL}/janus/api/queryPasswordEncrypt`;
  const headers = buildHeaders({ ...input, cookies }, url);
  delete headers.origin;
  delete headers["content-type"];
  return {
    method: "GET",
    url,
    headers,
    cookies,
  };
}

/**
 * Generate riskSign string.
 *
 * From PDD source (signLoginParams):
 *   riskSign = sign("username=<plain>&password=<plain>&ts=<timestamp>", antiContent)
 *
 * The signing function (SLhE.c / I) checks if the second argument has a
 * passwordEncrypt property. When it's a plain string (anti-content), it
 * returns the first argument unchanged.
 */
function buildRiskSign(username, password, timestamp) {
  return `username=${username || ""}&password=${password || ""}&ts=${timestamp || Date.now()}`;
}

/**
 * Build the complete auth (password login) request.
 */
function buildPasswordAuthRequest(options) {
  const input = options || {};
  const cookies = buildCookies(input);
  const url = `${BASE_URL}/janus/api/auth`;

  const fingerprintEnv = input.fingerprintEnv || randomizeFingerprint();
  const antiContent = input.antiContent || buildAntiContent({
    fingerprint: buildFingerprint(fingerprintEnv),
    cookies,
  });

  const baseHeaders = buildHeaders({ ...input, cookies, antiContent }, url);
  const headers = {
    ...baseHeaders,
    "content-type": "application/json",
  };

  // Ensure etag is set
  const etag = resolveRiskControlFp({ ...input, cookies });
  if (etag) headers["etag"] = etag;

  const body = {
    username: input.username || "",
    password: input.encryptedPassword || input.password || "",
    passwordEncrypt: Boolean(input.passwordEncrypt),
    verificationCode: input.verificationCode || "",
    mobileVerifyCode: input.mobileVerifyCode || "",
    sign: input.sign || "",
    touchevent: input.touchevent || {},
    fingerprint: input.fingerprint || buildFingerprint(fingerprintEnv),
    riskSign: input.riskSign || "",
    timestamp: input.timestamp || Date.now(),
    crawlerInfo: antiContent,
  };

  return {
    method: "POST",
    url,
    headers,
    cookies,
    body,
  };
}

async function main() {
  const action = process.argv[2];
  const input = await readStdinJson();

  const handlers = {
    "build-auth-request": buildPasswordAuthRequest,
    "build-risk-sign": (payload) => ({
      riskSign: buildRiskSign(
        payload.username,
        payload.password || payload.encryptedPassword,
        payload.timestamp || Date.now()
      ),
      timestamp: payload.timestamp || Date.now(),
    }),
    "query-password-encrypt": buildQueryPasswordEncryptRequest,
    "anti-content": async (payload) => ({
      antiContent: await buildAntiContent({
        fingerprint: buildFingerprint(
          (payload || {}).fingerprintEnv || randomizeFingerprint()
        ),
        cookies: buildCookies(payload || {}),
      }),
    }),
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
  buildRiskSign,
  buildPasswordAuthRequest,
  buildQueryPasswordEncryptRequest,
};
