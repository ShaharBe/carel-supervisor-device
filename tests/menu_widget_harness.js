const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class FakeElement {
  constructor(id = '') {
    this.id = id;
    this.textContent = '';
    this.className = '';
    this.hidden = false;
    this.disabled = false;
    this.value = '';
    this.title = '';
    this.type = '';
    this.children = [];
    this.attributes = new Map();
    this.listeners = new Map();
    const styleProperties = new Map();
    this.style = {
      setProperty: (name, value) => {
        styleProperties.set(name, String(value));
      },
      getPropertyValue: (name) => styleProperties.get(name) || '',
      gridTemplateRows: ''
    };
    this.classList = {
      add: () => {},
      remove: () => {}
    };
  }

  replaceChildren(...children) {
    this.children = [...children];
  }

  appendChild(child) {
    this.children.push(child);
    return child;
  }

  setAttribute(name, value) {
    this.attributes.set(name, value);
  }

  addEventListener(type, handler) {
    if (!this.listeners.has(type)) {
      this.listeners.set(type, []);
    }
    this.listeners.get(type).push(handler);
  }

  querySelector() {
    return new FakeElement();
  }

  querySelectorAll() {
    return [];
  }

  focus() {}

  select() {}
}

function collectDashboardSyncMap(root) {
  const syncMap = {};
  const stack = [root];
  while (stack.length > 0) {
    const node = stack.pop();
    if (node?.path && node.dashboard_sync) {
      syncMap[node.path] = node.dashboard_sync;
    }
    for (const child of node?.children || []) {
      stack.push(child);
    }
  }
  return syncMap;
}

function buildPayload() {
  const root = JSON.parse(
    fs.readFileSync(path.join(__dirname, '..', 'app', 'data', 'display_menu.json'), 'utf8')
  );
  return {
    ok: true,
    error: null,
    source_path: 'test',
    dashboard_sync_map: collectDashboardSyncMap(root),
    root
  };
}

function createDocument(payload) {
  const ids = [
    'cancelMenuEditBtn',
    'displayMenuData',
    'menuBackBtn',
    'menuDetail',
    'menuEditChoiceGroup',
    'menuEditModalBackdrop',
    'menuEditModalCurrent',
    'menuEditModalPath',
    'menuEditModalStatus',
    'menuEditModalTitle',
    'menuEditNumberField',
    'menuEditNumberInput',
    'menuEditSelectField',
    'menuEditSelectInput',
    'menuFontFamilySelect',
    'menuFontSizeRange',
    'menuFontSizeValue',
    'menuFontWidthRange',
    'menuFontWidthValue',
    'menuHomeBtn',
    'menuPageNextBtn',
    'menuPagePrevBtn',
    'menuScreen',
    'menuWidgetPath',
    'menuWidgetState',
    'resetMenuDisplayBtn',
    'saveMenuEditBtn'
  ];
  const nodes = new Map(ids.map((id) => [id, new FakeElement(id)]));
  nodes.get('displayMenuData').textContent = JSON.stringify(payload);
  nodes.get('menuFontFamilySelect').value = 'current';
  nodes.get('menuFontSizeRange').value = '100';
  nodes.get('menuFontWidthRange').value = '88';

  return {
    head: new FakeElement('head'),
    getElementById(id) {
      if (!nodes.has(id)) {
        nodes.set(id, new FakeElement(id));
      }
      return nodes.get(id);
    },
    createElement(tagName) {
      return new FakeElement(tagName);
    },
    querySelector() {
      return new FakeElement('query');
    },
    querySelectorAll() {
      return [];
    }
  };
}

function createStorage(store = new Map()) {
  return {
    getItem(key) {
      return store.has(key) ? store.get(key) : null;
    },
    setItem(key, value) {
      store.set(key, String(value));
    },
    removeItem(key) {
      store.delete(key);
    },
    clear() {
      store.clear();
    }
  };
}

function createWindow(document, options = {}) {
  return {
    document,
    localStorage: options.localStorage || createStorage(options.localStorageStore),
    sessionStorage: options.sessionStorage || createStorage(options.sessionStorageStore),
    matchMedia: () => ({ matches: false }),
    setTimeout,
    clearTimeout,
    addEventListener: () => {}
  };
}

function createDefaultFetchResponse(url) {
  const parsed = new URL(String(url), 'http://localhost/');
  return {
    json: async () => ({
      ok: true,
      path: parsed.searchParams.get('path'),
      value: 0
    })
  };
}

function createMenuWidgetHarness(options = {}) {
  const payload = options.payload || buildPayload();
  const document = createDocument(payload);
  const window = createWindow(document, options);
  const fetchCalls = [];
  const fetchImpl = options.fetchImpl || (async (url) => createDefaultFetchResponse(url));

  const wrappedFetch = async (url, requestOptions) => {
    fetchCalls.push(String(url));
    return fetchImpl(url, requestOptions);
  };

  const sandbox = {
    window,
    document,
    fetch: wrappedFetch,
    URL,
    URLSearchParams,
    console,
    JSON,
    Map,
    Set
  };

  window.fetch = wrappedFetch;
  vm.runInNewContext(
    fs.readFileSync(path.join(__dirname, '..', 'app', 'static', 'menu-widget.js'), 'utf8'),
    sandbox,
    { filename: 'menu-widget.js' }
  );

  const widget = window.CarelMenuWidget;
  return {
    payload,
    widget,
    document,
    window,
    fetchCalls,
    clearFetchCalls() {
      fetchCalls.length = 0;
    }
  };
}

module.exports = {
  createMenuWidgetHarness
};
