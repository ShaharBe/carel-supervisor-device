const test = require('node:test');
const assert = require('node:assert/strict');
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
    this.style = {
      setProperty: () => {},
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

function createWindow(document) {
  return {
    document,
    localStorage: {
      getItem: () => null,
      setItem: () => {}
    },
    matchMedia: () => ({ matches: false }),
    addEventListener: () => {}
  };
}

function buildPayload() {
  const root = JSON.parse(
    fs.readFileSync(path.join(__dirname, '..', 'app', 'data', 'display_menu.json'), 'utf8')
  );
  return {
    ok: true,
    error: null,
    source_path: 'test',
    dashboard_sync_map: {},
    root
  };
}

test('dashboard refresh re-fetches visible remote leaves in the active menu', async () => {
  const payload = buildPayload();
  const document = createDocument(payload);
  const window = createWindow(document);
  const fetchCalls = [];

  global.document = document;
  global.window = window;
  global.fetch = async (url) => {
    fetchCalls.push(String(url));
    const parsed = new URL(String(url), 'http://localhost/');
    const pathValue = parsed.searchParams.get('path');
    return {
      json: async () => ({
        ok: true,
        path: pathValue,
        value: 0
      })
    };
  };

  const script = fs.readFileSync(path.join(__dirname, '..', 'app', 'static', 'menu-widget.js'), 'utf8');
  vm.runInThisContext(script, { filename: 'menu-widget.js' });

  const widget = window.CarelMenuWidget;
  widget.init();
  widget.__testing.setCurrentMenuPath('3.2.2');

  fetchCalls.length = 0;

  await widget.handleDashboardRefresh({ ok: true, info: {}, alarms: {} });

  assert.ok(
    fetchCalls.some((url) => url.includes('path=3.2.2.2')),
    `expected visible leaf 3.2.2.2 to be refreshed, got: ${fetchCalls.join(', ')}`
  );
});
