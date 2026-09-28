// Run after `npm ci --prefix web`: node --test checks/frontend_chat_unit.cjs
// Execute the actual TSX with a small hook scheduler; these tests exercise state
// and async response ordering, not browser layout or React DOM integration.
const assert = require("node:assert/strict");
const { test } = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const ts = require("../web/node_modules/typescript");

const sourceRoot = path.resolve(__dirname, "../web/src");
const shops = [{ id: 1, name: "shop", status: "online" }];
const conversations = [
  { id: 1, shop_id: 1, updated_at: "2026-09-10T10:00:00Z" },
  { id: 2, shop_id: 1, updated_at: "2026-09-10T09:00:00Z" },
];
const sameDeps = (a, b) => a && b && a.length === b.length && a.every((value, i) => Object.is(value, b[i]));
const response = (data) => ({ ok: true, json: async () => data });
function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}

function harness(relativePath, exportName, initialProps = {}) {
  let props = initialProps;
  let cursor = 0;
  let dirty = true;
  let tree;
  const slots = [];
  let effects = [];
  const handlers = new Map();
  const sockets = [];
  const timers = new Map();
  let timerId = 0;
  const react = {
    useState(initial) {
      const index = cursor++;
      if (!slots[index]) slots[index] = { value: typeof initial === "function" ? initial() : initial };
      return [slots[index].value, (update) => {
        const next = typeof update === "function" ? update(slots[index].value) : update;
        if (!Object.is(next, slots[index].value)) { slots[index].value = next; dirty = true; }
      }];
    },
    useRef(initial) {
      const index = cursor++;
      if (!slots[index]) slots[index] = { current: initial };
      return slots[index];
    },
    useMemo(compute, deps) {
      const index = cursor++;
      if (!slots[index] || !sameDeps(slots[index].deps, deps)) slots[index] = { value: compute(), deps };
      return slots[index].value;
    },
    useCallback(callback, deps) { return react.useMemo(() => callback, deps); },
    useEffect(callback, deps) {
      const index = cursor++;
      if (!slots[index] || !sameDeps(slots[index].deps, deps)) {
        const previous = slots[index];
        slots[index] = { deps };
        effects.push(() => { previous?.cleanup?.(); slots[index].cleanup = callback(); });
      }
    },
  };
  const components = new Proxy({}, { get: (_, key) => key });
  const jsx = (type, props) => ({ type, props: props || {} });
  const globals = {
    console, Headers, FormData, URL, URLSearchParams,
    localStorage: { getItem: () => "test-token", setItem() {}, removeItem() {} },
    location: { protocol: "http:", host: "test.invalid" },
    window: {
      setTimeout(callback) { const id = ++timerId; timers.set(id, callback); return id; },
      clearTimeout(id) { timers.delete(id); },
      setInterval() { return ++timerId; }, clearInterval() {},
    },
    WebSocket: class { constructor() { sockets.push(this); } close() {} },
    fetch: async (url, init) => {
      if (handlers.has(url)) return handlers.get(url)(init);
      if (url === "/api/me") return response({ id: 1, role: "user", username: "review", display_name: "Review" });
      if (url === "/api/shops") return response(shops);
      if (url === "/api/conversations") return response(conversations);
      const messages = url.match(/^\/api\/conversations\/(\d+)\/messages$/);
      if (messages) return response([{ id: Number(messages[1]), conversation_id: Number(messages[1]) }]);
      const context = url.match(/^\/api\/conversations\/(\d+)\/context$/);
      if (context) return response({ conversation_id: Number(context[1]) });
      return response([]);
    },
  };
  const moduleCache = new Map();
  function load(file) {
    if (moduleCache.has(file)) return moduleCache.get(file);
    const compiled = ts.transpileModule(fs.readFileSync(file, "utf8"), {
      compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020, jsx: ts.JsxEmit.ReactJSX },
    }).outputText;
    const module = { exports: {} };
    moduleCache.set(file, module.exports);
    const localRequire = (name) => {
      if (name === "react") return react;
      if (name === "react/jsx-runtime") return { jsx, jsxs: jsx };
      if (name === "lucide-react" || name.includes("components/") || name === "./StatusBadge") return components;
      const base = path.resolve(path.dirname(file), name);
      return load([`${base}.ts`, `${base}.tsx`].find((candidate) => fs.existsSync(candidate)));
    };
    vm.runInNewContext(compiled, { ...globals, require: localRequire, module, exports: module.exports }, { filename: file });
    return module.exports;
  }
  const Component = load(path.join(sourceRoot, relativePath))[exportName];
  const api = {
    handlers, sockets,
    setProps(next) { props = next; dirty = true; },
    render() {
      cursor = 0; dirty = false; tree = Component(props);
      const pending = effects; effects = []; pending.forEach((effect) => effect());
    },
    async settle() {
      for (let i = 0; i < 35; i++) { if (dirty) api.render(); await Promise.resolve(); }
    },
    flushTimers() {
      const pending = [...timers.values()]; timers.clear(); pending.forEach((callback) => callback());
    },
    find(type, predicate = () => true) {
      function visit(node) {
        if (Array.isArray(node)) return node.map(visit).find(Boolean);
        if (!node || typeof node !== "object") return undefined;
        if (node.type === type && predicate(node.props)) return node;
        return visit(node.props?.children);
      }
      const node = visit(tree);
      assert.ok(node, `Expected ${type} in rendered tree`);
      return node.props;
    },
  };
  return api;
}

async function appHarness() {
  const app = harness("App.tsx", "App");
  await app.settle();
  app.find("NavButton", (props) => props.label === "客服聊天").onClick();
  await app.settle();
  return app;
}

test("online opens the verification modal when auto relogin needs an SMS code", async () => {
  const app = harness("App.tsx", "App");
  await app.settle();
  app.handlers.set("/api/shops/1/online", () => response({
    status: "verification_required",
    need_verify: true,
    verify_type: "mobile",
    mask_mobile: "138****0000",
    message: "请输入短信验证码后继续登录",
  }));

  await app.find("ShopPage").onOnline(1);
  await app.settle();

  const modal = app.find("PasswordLoginModal");
  assert.equal(modal.shop.id, 1);
  assert.equal(modal.initialVerifyInfo.verify_type, "mobile");
  assert.equal(modal.initialVerifyInfo.mask_mobile, "138****0000");
});

test("an initial verification challenge shows the code input while login is pending", async () => {
  const modal = harness("components/PasswordLoginModal.tsx", "PasswordLoginModal", {
    shop: { id: 1, name: "shop", status: "login_pending" },
    initialVerifyInfo: { verify_type: "mobile", mask_mobile: "138****0000" },
    onLogin: async () => ({}),
    onSendSms: async () => {},
    onVerify: async () => {},
    onClose: () => {},
  });
  await modal.settle();
  assert.equal(modal.find("input", (props) => props.placeholder === "输入 6 位验证码").maxLength, 6);
});

test("a shop in error state can retry online when it has login cache", async () => {
  const shopPage = harness("components/ShopPage.tsx", "ShopPage", {
    shops: [{ id: 1, name: "shop", status: "error", has_login_cache: true }],
    selectedShopId: 1,
    onSelect: () => {},
    onCreate: async () => {},
    onLogin: async () => {},
    onPasswordLogin: () => {},
    onOnline: async () => {},
    onOffline: async () => {},
    onDelete: async () => {},
    onToggleAutoReply: async () => {},
    onOpenTransferSettings: () => {},
  });
  await shopPage.settle();
  const onlineButton = shopPage.find("button", (props) => (
    Array.isArray(props.children) && props.children.includes("上线")
  ));
  assert.equal(onlineButton.disabled, false);
});

test("incoming message preserves the actively selected conversation", async () => {
  const app = await appHarness();
  app.find("ChatPage").onSelectConversation(2);
  await app.settle();
  app.sockets[0].onmessage({ data: JSON.stringify({ type: "message", data: { id: 99, shop_id: 1, conversation_id: 1 } }) });
  app.flushTimers();
  await app.settle();
  assert.equal(app.find("ChatPage").selectedConversation.id, 2);
});

test("late messages and context from another conversation do not replace the current view", async () => {
  const app = await appHarness();
  const messages = deferred();
  const context = deferred();
  app.handlers.set("/api/conversations/2/messages", () => messages.promise);
  app.handlers.set("/api/conversations/2/context", () => context.promise);
  app.find("ChatPage").onSelectConversation(2);
  await app.settle();
  app.find("ChatPage").onSelectConversation(1);
  await app.settle();
  messages.resolve(response([{ id: 22, conversation_id: 2 }]));
  context.resolve(response({ conversation_id: 2 }));
  await app.settle();
  const chat = app.find("ChatPage");
  assert.equal(chat.messages[0].conversation_id, 1);
  assert.equal(chat.customerContext.conversation_id, 1);
});

test("late conversation list does not overwrite a newer shop filter", async () => {
  const app = await appHarness();
  const stale = deferred();
  app.handlers.set("/api/conversations", () => stale.promise);
  app.sockets[0].onmessage({ data: JSON.stringify({ type: "message", data: { id: 99, shop_id: 1, conversation_id: 1 } }) });
  app.flushTimers();
  await app.settle();
  app.handlers.set("/api/conversations?shop_id=1", () => response([conversations[1]]));
  app.find("ChatPage").onShopFilterChange(1);
  await app.settle();
  stale.resolve(response(conversations));
  await app.settle();
  assert.equal(app.find("ChatPage").conversations.length, 1);
  assert.equal(app.find("ChatPage").selectedConversation.id, 2);
});

test("a delayed context timer from a previous conversation cannot invalidate the current request", async () => {
  const app = await appHarness();
  const context = deferred();
  app.handlers.set("/api/conversations/2/context", () => context.promise);
  app.sockets[0].onmessage({ data: JSON.stringify({ type: "message", data: { id: 99, shop_id: 1, conversation_id: 1 } }) });
  app.find("ChatPage").onSelectConversation(2);
  await app.settle();
  app.flushTimers();
  await app.settle();
  context.resolve(response({ conversation_id: 2 }));
  await app.settle();
  assert.equal(app.find("ChatPage").customerContext.conversation_id, 2);
});

function chatProps(id, onReply = async () => {}) {
  return { shops, activeShop: shops[0], shopFilter: "all", conversations, selectedConversation: conversations[id - 1],
    messages: [], customerContext: null, services: [], onReply };
}
const input = (chat) => chat.find("input", (props) => typeof props.value === "string" && props.placeholder === "输入人工回复");

test("drafts are isolated by conversation and restored when switching back", async () => {
  const chat = harness("components/ChatPage.tsx", "ChatPage", chatProps(1));
  await chat.settle();
  input(chat).onChange({ target: { value: "reply for buyer one" } });
  await chat.settle();
  chat.setProps(chatProps(2));
  await chat.settle();
  assert.equal(input(chat).value, "");
  input(chat).onChange({ target: { value: "reply for buyer two" } });
  await chat.settle();
  chat.setProps(chatProps(1));
  await chat.settle();
  assert.equal(input(chat).value, "reply for buyer one");
});

test("completion of an earlier send cannot clear another conversation's draft", async () => {
  const sending = deferred();
  const chat = harness("components/ChatPage.tsx", "ChatPage", chatProps(1, () => sending.promise));
  await chat.settle();
  input(chat).onChange({ target: { value: "first reply" } });
  await chat.settle();
  const submit = chat.find("form").onSubmit({ preventDefault() {} });
  chat.setProps(chatProps(2));
  await chat.settle();
  input(chat).onChange({ target: { value: "keep second draft" } });
  await chat.settle();
  sending.resolve();
  await submit;
  await chat.settle();
  assert.equal(input(chat).value, "keep second draft");
  chat.setProps(chatProps(1));
  await chat.settle();
  assert.equal(input(chat).value, "");
});
