"use strict";

function createWebpackRequire(modules) {
  const cache = {};

  function req(id) {
    if (cache[id]) {
      return cache[id].exports;
    }
    if (!modules[id]) {
      throw new Error(`missing webpack module: ${id}`);
    }
    const module = cache[id] = {exports: {}};
    modules[id](module, module.exports, req);
    return module.exports;
  }

  req.d = (exports, name, getter) => {
    if (!Object.prototype.hasOwnProperty.call(exports, name)) {
      Object.defineProperty(exports, name, {enumerable: true, get: getter});
    }
  };
  req.r = (exports) => {
    Object.defineProperty(exports, "__esModule", {value: true});
  };
  req.n = (module) => {
    const getter = module && module.__esModule ? () => module.default : () => module;
    req.d(getter, "a", getter);
    return getter;
  };
  req.t = (value, mode) => {
    if (mode & 1) {
      value = req(value);
    }
    if (mode & 8) {
      return value;
    }
    const ns = {};
    req.r(ns);
    Object.defineProperty(ns, "default", {enumerable: true, value});
    return ns;
  };
  req.o = (obj, prop) => Object.prototype.hasOwnProperty.call(obj, prop);
  req.e = (id) => Promise.reject(new Error(`chunk loading disabled: ${id}`));
  return req;
}

module.exports = {
  createWebpackRequire,
};
