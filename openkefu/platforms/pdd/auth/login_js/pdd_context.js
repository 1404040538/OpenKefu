"use strict";

const crypto = require("crypto");
const {buildAntiContent} = require("./anti_content");

const BASE_URL = "https://mms.pinduoduo.com";
const XG_BASE_URL = "https://xg.pinduoduo.com";
const LOGIN_URL =
  "https://mms.pinduoduo.com/login/?redirectUrl=https%3A%2F%2Fmms.pinduoduo.com%2Fhome%2F";

const RISK_FP_COOKIE_NAMES = ["rckk", "_bee"];
const UUID_COOKIE_PAIRS = [
  ["ru1k", "_f77"],
  ["ru2k", "_a42"],
];

// 这些值需要与 anti_content.js 使用的浏览器画像保持一致。
const DEFAULT_ENV = {
  innerHeight: 945,
  innerWidth: 1430,
  devicePixelRatio: 1,
  availHeight: 1032,
  availWidth: 1920,
  height: 1080,
  width: 1920,
  colorDepth: 32,
  locationHref: LOGIN_URL,
  clientWidth: 1430,
  clientHeight: 945,
  offsetWidth: 1430,
  offsetHeight: 945,
  scrollWidth: 2743,
  scrollHeight: 945,
  referer: "",
  timezoneOffset: -480,
  navigator: {
    appCodeName: "Mozilla",
    appName: "Netscape",
    hardwareConcurrency: 28,
    language: "zh-CN",
    cookieEnabled: true,
    platform: "Win32",
    doNotTrack: null,
    ua: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36 Edg/148.0.0.0",
    vendor: "Google Inc.",
    product: "Gecko",
    productSub: "20030107",
    mimeTypes: "f5a1111231f589322da33fb59b56946b4043e092",
    plugins: "387b918f593d4d8d6bfa647c07e108afbd7a6223",
  },
};

const ANTI_HEADER_DOMAIN_RE = /https?:\/\/(mms|ims|ipp|jubao-api|topen-api|shuyuan|imsapi|open-api|jinbao|open|mai|dmp|icube|wb|mch|brandside).+?\.(com|net)/;
const ANTI_HEADER_TEST_RE = /https?:\/\/test(ing|2)\.hutaojie(.+?)?\.com/;

function randomText(length, alphabet) {
  let output = "";
  const bytes = crypto.randomBytes(length);
  for (const byte of bytes) {
    output += alphabet[byte % alphabet.length];
  }
  return output;
}

function makeNanoFp() {
  const alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz_~";
  return `Xpm8${randomText(8, alphabet)}X${randomText(8, alphabet)}_${randomText(20, alphabet)}`;
}

function makeRiskControlFp(seed) {
  return seed || randomText(32, "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz");
}

function buildCookies(options) {
  const input = options || {};
  const cookies = {...(input.cookies || {})};
  const riskControlFp = makeRiskControlFp(input.riskControlFp || cookies.rckk || cookies._bee);

  if (!cookies._nano_fp) {
    cookies._nano_fp = makeNanoFp();
  }
  for (const name of RISK_FP_COOKIE_NAMES) {
    if (!cookies[name] && riskControlFp) {
      cookies[name] = riskControlFp;
    }
  }
  for (const [left, right] of UUID_COOKIE_PAIRS) {
    const value = cookies[left] || cookies[right] || crypto.randomUUID();
    cookies[left] = value;
    cookies[right] = value;
  }
  return cookies;
}

function mergeDeep(base, extra) {
  const output = {...base};
  for (const [key, value] of Object.entries(extra || {})) {
    if (
      value &&
      typeof value === "object" &&
      !Array.isArray(value) &&
      base[key] &&
      typeof base[key] === "object" &&
      !Array.isArray(base[key])
    ) {
      output[key] = mergeDeep(base[key], value);
    } else {
      output[key] = value;
    }
  }
  return output;
}

function buildFingerprint(env) {
  const fp = mergeDeep(DEFAULT_ENV, env || {});
  return {
    innerHeight: fp.innerHeight,
    innerWidth: fp.innerWidth,
    devicePixelRatio: fp.devicePixelRatio,
    availHeight: fp.availHeight,
    availWidth: fp.availWidth,
    height: fp.height,
    width: fp.width,
    colorDepth: fp.colorDepth,
    locationHref: fp.locationHref,
    clientWidth: fp.clientWidth,
    clientHeight: fp.clientHeight,
    offsetWidth: fp.offsetWidth,
    offsetHeight: fp.offsetHeight,
    scrollWidth: fp.scrollWidth,
    scrollHeight: fp.scrollHeight,
    navigator: {...fp.navigator},
    referer: fp.referer,
    timezoneOffset: fp.timezoneOffset,
  };
}

function isRelativeURL(url = "") {
  if (typeof url !== "string") {
    throw new Error("-- The type of url MUST be [object String]. --");
  }
  return !/^https?:\/\//.test(url) && !/^\/\//.test(url);
}

function shouldAddAntiHeader(url = "") {
  return isRelativeURL(url) || ANTI_HEADER_DOMAIN_RE.test(url) || ANTI_HEADER_TEST_RE.test(url);
}

function randomInt(min, max) {
  return Math.floor(Math.random() * (max - min + 1)) + min;
}

function pickRandom(arr) {
  return arr[randomInt(0, arr.length - 1)];
}

const RESOLUTION_POOLS = [
  { width: 1920, height: 1080, innerW: 1430, innerH: 945, availW: 1920, availH: 1032 },
  { width: 1680, height: 1050, innerW: 1320, innerH: 890, availW: 1680, availH: 1002 },
  { width: 1600, height: 900, innerW: 1250, innerH: 740, availW: 1600, availH: 852 },
  { width: 1536, height: 864, innerW: 1200, innerH: 710, availW: 1536, availH: 816 },
  { width: 1440, height: 900, innerW: 1100, innerH: 740, availW: 1440, availH: 852 },
  { width: 1366, height: 768, innerW: 1036, innerH: 618, availW: 1366, availH: 720 },
];

const CHROME_MAJOR_VERSIONS = [148];
const HARDWARE_CONCURRENCY_POOL = [4, 8, 12, 16, 20, 24, 28, 32];
const COLOR_DEPTH_POOL = [24, 32];
const DEVICE_PIXEL_RATIO_POOL = [1, 1.25, 1.5, 2];

function randomizeFingerprint() {
  const res = pickRandom(RESOLUTION_POOLS);
  const dpr = pickRandom(DEVICE_PIXEL_RATIO_POOL);
  const major = pickRandom(CHROME_MAJOR_VERSIONS);
  const concurrency = pickRandom(HARDWARE_CONCURRENCY_POOL);
  const colorDepth = pickRandom(COLOR_DEPTH_POOL);

  const uaTemplate = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/" + major + ".0.0.0 Safari/537.36 Edg/" + major + ".0.0.0";

  return {
    innerHeight: res.innerH,
    innerWidth: res.innerW,
    devicePixelRatio: dpr,
    availHeight: res.availH,
    availWidth: res.availW,
    height: res.height,
    width: res.width,
    colorDepth: colorDepth,
    clientWidth: res.innerW,
    clientHeight: res.innerH,
    offsetWidth: res.innerW,
    offsetHeight: res.innerH,
    scrollWidth: res.innerW + randomInt(1000, 2500),
    scrollHeight: res.innerH,
    timezoneOffset: pickRandom([-480, -540, -600, -660, -720]),
    navigator: {
      appCodeName: "Mozilla",
      appName: "Netscape",
      hardwareConcurrency: concurrency,
      language: "zh-CN",
      cookieEnabled: true,
      platform: "Win32",
      doNotTrack: null,
      ua: uaTemplate,
      vendor: "Google Inc.",
      product: "Gecko",
      productSub: "20030107",
      mimeTypes: "f5a1111231f589322da33fb59b56946b4043e092",
      plugins: "387b918f593d4d8d6bfa647c07e108afbd7a6223",
    },
  };
}

function resolveRiskControlFp(options) {
  if (options.riskControlFp) {
    return options.riskControlFp;
  }
  const cookies = options.cookies || {};
  return cookies.rckk || cookies._bee || "";
}

function buildHeaders(options, url = BASE_URL) {
  const cookies = buildCookies(options || {});
  const fingerprint = buildFingerprint((options || {}).fingerprintEnv);
  const ua = fingerprint.navigator && fingerprint.navigator.ua || DEFAULT_ENV.navigator.ua;
  const chromeVersion = (ua.match(/Chrome\/(\d+)/) || [])[1] || "147";
  const headers = {
    accept: "*/*",
    "accept-language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
    "cache-control": "no-cache",
    "content-type": "application/json",
    origin: BASE_URL,
    pragma: "no-cache",
    priority: "u=1, i",
    referer: LOGIN_URL,
    "sec-ch-ua": "\"Chromium\";v=\"" + chromeVersion + "\", \"Microsoft Edge\";v=\"" + chromeVersion + "\", \"Not/A)Brand\";v=\"99\"",
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": "\"Windows\"",
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
    "user-agent": ua,
  };

  const eTag = resolveRiskControlFp({...options, cookies});
  if (eTag) {
    headers.etag = eTag;
  }
  if (shouldAddAntiHeader(url)) {
    headers["anti-content"] = options && options.antiContent
      ? options.antiContent
      : buildAntiContent({fingerprint, cookies});
  }
  const verifyAuthToken = (options && options.verifyAuthToken) || (options && options.verifyauthtoken);
  if (verifyAuthToken) {
    headers.verifyauthtoken = verifyAuthToken;
    headers.VerifyAuthToken = verifyAuthToken;
  }
  return headers;
}

function buildPfbHeaders(options) {
  const url = `${XG_BASE_URL}/xg/pfb/a2`;
  const fingerprint = buildFingerprint((options || {}).fingerprintEnv);
  const ua = fingerprint.navigator && fingerprint.navigator.ua || DEFAULT_ENV.navigator.ua;
  const chromeVersion = (ua.match(/Chrome\/(\d+)/) || [])[1] || "147";
  const headers = {
    accept: "application/json, text/plain, */*",
    "accept-language": "zh-CN,zh;q=0.9",
    "cache-control": "no-cache",
    "content-type": "application/json",
    origin: BASE_URL,
    pragma: "no-cache",
    priority: "u=1, i",
    referer: `${BASE_URL}/`,
    "sec-ch-ua": "\"Google Chrome\";v=\"" + chromeVersion + "\", \"Not.A/Brand\";v=\"8\", \"Chromium\";v=\"" + chromeVersion + "\"",
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": "\"Windows\"",
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-site",
    "user-agent": ua,
  };
  if (options && options.antiContent && shouldAddAntiHeader(url)) {
    headers["Anti-Content"] = options.antiContent;
  }
  return headers;
}

module.exports = {
  BASE_URL,
  XG_BASE_URL,
  LOGIN_URL,
  buildCookies,
  buildFingerprint,
  buildHeaders,
  buildPfbHeaders,
  resolveRiskControlFp,
  shouldAddAntiHeader,
  randomizeFingerprint,
};
