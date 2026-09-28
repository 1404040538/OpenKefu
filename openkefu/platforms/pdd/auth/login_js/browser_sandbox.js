"use strict";

const LOGIN_URL =
  "https://mms.pinduoduo.com/login/?redirectUrl=https%3A%2F%2Fmms.pinduoduo.com%2Fhome%2F";

function cookieString(cookies) {
  return Object.entries(cookies || {})
    .filter(([, value]) => value !== undefined && value !== null && value !== "")
    .map(([key, value]) => `${key}=${value}`)
    .join("; ");
}

function createStorage() {
  const store = new Map();
  return {
    get length() {
      return store.size;
    },
    key(index) {
      return Array.from(store.keys())[index] || null;
    },
    getItem(key) {
      key = String(key);
      return store.has(key) ? store.get(key) : null;
    },
    setItem(key, value) {
      store.set(String(key), String(value));
    },
    removeItem(key) {
      store.delete(String(key));
    },
    clear() {
      store.clear();
    },
  };
}

function makeArrayLike(value) {
  if (Array.isArray(value)) {
    return value;
  }
  if (typeof value === "string" && value) {
    return [{name: value, filename: value, description: value}];
  }
  return [];
}

function createBrowserDate(timezoneOffset) {
  function BrowserDate(...args) {
    if (!(this instanceof BrowserDate)) {
      return Date(...args);
    }
    return args.length ? new Date(...args) : new Date();
  }
  Object.setPrototypeOf(BrowserDate, Date);
  BrowserDate.prototype = Object.create(Date.prototype);
  BrowserDate.prototype.constructor = BrowserDate;
  BrowserDate.prototype.getTimezoneOffset = function() {
    return timezoneOffset;
  };
  BrowserDate.now = () => Date.now();
  BrowserDate.parse = Date.parse;
  BrowserDate.UTC = Date.UTC;
  return BrowserDate;
}

function createBrowserContext(options) {
  const input = options || {};
  const fp = input.fingerprint || {};
  const navFp = fp.navigator || {};
  const modules = {};
  const now = input.serverTime || Date.now();
  const timezoneOffset = fp.timezoneOffset ?? -480;
  const BrowserDate = createBrowserDate(timezoneOffset);
  const performance = {
    now: () => Date.now() - now,
    timing: {
      navigationStart: now,
    },
  };
  const navigator = {
    userAgent: navFp.ua || "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
    appCodeName: navFp.appCodeName || "Mozilla",
    appName: navFp.appName || "Netscape",
    hardwareConcurrency: navFp.hardwareConcurrency || 28,
    language: navFp.language || "zh-CN",
    cookieEnabled: navFp.cookieEnabled !== false,
    platform: navFp.platform || "Win32",
    doNotTrack: navFp.doNotTrack || null,
    vendor: navFp.vendor || "Google Inc.",
    product: navFp.product || "Gecko",
    productSub: navFp.productSub || "20030107",
    mimeTypes: makeArrayLike(navFp.mimeTypes),
    plugins: makeArrayLike(navFp.plugins),
  };
  const history = {
    back() {},
    pushState() {},
    replaceState() {},
  };
  const location = {
    href: fp.locationHref || LOGIN_URL,
    origin: "https://mms.pinduoduo.com",
    hostname: "mms.pinduoduo.com",
    host: "mms.pinduoduo.com",
    pathname: "/login/",
    search: "?redirectUrl=https%3A%2F%2Fmms.pinduoduo.com%2Fhome%2F",
  };
  const screen = {
    availHeight: fp.availHeight || 1032,
    availWidth: fp.availWidth || 1920,
    height: fp.height || 1080,
    width: fp.width || 1920,
    colorDepth: fp.colorDepth || 32,
  };

  function XHR() {
    this.readyState = 0;
    this.status = 200;
    this.responseText = JSON.stringify({server_time: now});
  }
  XHR.prototype.open = function() {};
  XHR.prototype.setRequestHeader = function() {};
  XHR.prototype.getResponseHeader = function(name) {
    return String(name).toLowerCase() === "date" ? new Date(now).toUTCString() : "";
  };
  XHR.prototype.send = function() {
    this.readyState = 4;
    if (this.onreadystatechange) {
      this.onreadystatechange();
    }
  };

  const documentElement = {
    clientWidth: fp.clientWidth || fp.innerWidth || 1430,
    clientHeight: fp.clientHeight || fp.innerHeight || 945,
    scrollWidth: fp.scrollWidth || fp.clientWidth || fp.innerWidth || 1430,
    scrollHeight: fp.scrollHeight || fp.clientHeight || fp.innerHeight || 945,
    scrollTop: 0,
    scrollLeft: 0,
    style: {},
  };
  const body = {
    clientWidth: fp.clientWidth || 1430,
    clientHeight: fp.clientHeight || 945,
    offsetWidth: fp.offsetWidth || fp.clientWidth || 1430,
    offsetHeight: fp.offsetHeight || fp.clientHeight || 945,
    scrollWidth: fp.scrollWidth || 2743,
    scrollHeight: fp.scrollHeight || 945,
    scrollTop: 0,
    scrollLeft: 0,
    style: {},
    appendChild(child) {
      return child;
    },
    removeChild(child) {
      return child;
    },
  };
  const document = {
    referrer: fp.referer || "",
    cookie: cookieString(input.cookies),
    body,
    documentElement,
    addEventListener() {},
    removeEventListener() {},
    createElement(tagName) {
      return {
        tagName: String(tagName || "").toUpperCase(),
        style: {},
        children: [],
        clientWidth: 0,
        clientHeight: 0,
        offsetWidth: 0,
        offsetHeight: 0,
        scrollWidth: 0,
        scrollHeight: 0,
        scrollLeft: 0,
        scrollTop: 0,
        setAttribute() {},
        appendChild(child) {
          this.children.push(child);
          return child;
        },
        removeChild(child) {
          this.children = this.children.filter((item) => item !== child);
          return child;
        },
        getBoundingClientRect() {
          return {left: 0, top: 0, width: this.clientWidth, height: this.clientHeight};
        },
        getContext() {
          return null;
        },
      };
    },
    head: {
      appendChild() {},
    },
  };

  const win = {
    webpackJsonp: {
      push(chunk) {
        Object.assign(modules, chunk[1]);
      },
    },
    navigator,
    performance,
    history,
    location,
    screen,
    document,
    innerHeight: fp.innerHeight || 945,
    innerWidth: fp.innerWidth || 1430,
    outerHeight: fp.outerHeight || fp.height || 1080,
    outerWidth: fp.outerWidth || fp.width || 1920,
    devicePixelRatio: fp.devicePixelRatio || 1,
    pageXOffset: 0,
    pageYOffset: 0,
    scrollX: 0,
    scrollY: 0,
    localStorage: createStorage(),
    sessionStorage: createStorage(),
    getComputedStyle() {
      return {
        getPropertyValue() {
          return "";
        },
      };
    },
    fetch: () => Promise.resolve({}),
    Date: BrowserDate,
    Math,
    JSON,
    Promise,
    setTimeout,
    clearTimeout,
    parseInt,
    isNaN,
    mmsCMT: {
      timeBaseline: {
        serverTime: now,
        localTime: now,
      },
      pendingPromoise: null,
    },
  };
  win.window = win;
  win.self = win;
  win.top = win;

  return {
    modules,
    context: {
      window: win,
      self: win,
      top: win,
      document,
      navigator,
      history,
      location,
      screen,
      performance,
      fetch: win.fetch,
      console,
      setTimeout,
      clearTimeout,
      Date: BrowserDate,
      Promise,
      Symbol,
      Math,
      Uint8Array,
      Uint16Array,
      Int32Array,
      ArrayBuffer,
      Error,
      RegExp,
      Object,
      Function,
      String,
      Number,
      Boolean,
      JSON,
      parseInt,
      isNaN,
      decodeURIComponent,
      encodeURIComponent,
      localStorage: win.localStorage,
      sessionStorage: win.sessionStorage,
      getComputedStyle: win.getComputedStyle,
      XMLHttpRequest: XHR,
    },
  };
}

module.exports = {
  createBrowserContext,
};
