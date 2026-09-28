"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const {createBrowserContext} = require("./browser_sandbox");
const {createWebpackRequire} = require("./webpack_runtime");

const CHUNK_FILE = path.join(__dirname, "0.5c978353.chunk.v20260122114222_a590dde8.js");

function loadRiskModule(options) {
  const sandbox = createBrowserContext(options);
  vm.createContext(sandbox.context);
  vm.runInContext(fs.readFileSync(CHUNK_FILE, "utf8"), sandbox.context);
  const req = createWebpackRequire(sandbox.modules);
  return req("YSLh");
}

function buildAntiContent(options) {
  const antiContent = loadRiskModule(options).syncGetRiskInfo();
  if (!antiContent) {
    throw new Error("syncGetRiskInfo returned empty Anti-Content");
  }
  return antiContent;
}

async function buildAntiContentAsync(options) {
  const antiContent = await loadRiskModule(options).getRiskInfo();
  if (!antiContent) {
    throw new Error("getRiskInfo returned empty Anti-Content");
  }
  return antiContent;
}

if (require.main === module) {
  let data = "";
  process.stdin.setEncoding("utf8");
  process.stdin.on("data", (chunk) => {
    data += chunk;
  });
  process.stdin.on("end", () => {
    const input = data.trim() ? JSON.parse(data) : {};
    buildAntiContentAsync(input)
      .then((antiContent) => {
        process.stdout.write(JSON.stringify({antiContent}));
      })
      .catch((err) => {
        process.stderr.write(err && err.stack ? err.stack : String(err));
        process.exit(1);
      });
  });
}

module.exports = {
  buildAntiContent,
  buildAntiContentAsync,
};
