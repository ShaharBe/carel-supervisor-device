const test = require('node:test');
const assert = require('node:assert/strict');
const { createMenuWidgetHarness } = require('./menu_widget_harness');

function createDeferred() {
  let resolve;
  let reject;
  const promise = new Promise((innerResolve, innerReject) => {
    resolve = innerResolve;
    reject = innerReject;
  });
  return { promise, resolve, reject };
}

function flushAsyncWork() {
  return new Promise((resolve) => setImmediate(resolve));
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function dispatchElementEvent(element, type, patch = {}) {
  const event = {
    type,
    target: element,
    currentTarget: element,
    pointerType: 'touch',
    pointerId: 1,
    clientX: 0,
    clientY: 0,
    preventDefault() {},
    stopPropagation() {},
    ...patch
  };

  for (const handler of element.listeners.get(type) || []) {
    handler(event);
  }
}

function buildMenuValueResponse(path, value, extra = {}) {
  return {
    json: async () => ({
      ok: true,
      path,
      value,
      ...extra
    })
  };
}

function createLongPressPayload() {
  return {
    ok: true,
    error: null,
    source_path: 'test',
    dashboard_sync_map: {},
    root: {
      path: '',
      title: 'Root',
      display_label: 'Root',
      raw_text: 'Root',
      kind: 'root',
      children: [
        {
          path: '1',
          title: 'Service',
          display_label: 'Service',
          raw_text: 'Service',
          kind: 'menu',
          children: [],
          register: null,
          range_or_options: null,
          note: null,
          is_caption: false,
          is_stub: false,
          page_direction: null
        },
        {
          path: '2',
          title: 'Editable Leaf',
          display_label: 'Editable Leaf',
          raw_text: 'Editable Leaf [yes,no]',
          kind: 'leaf',
          children: [],
          register: null,
          editor: {
            type: 'boolean',
            options: [
              { value: true, label: 'yes' },
              { value: false, label: 'no' }
            ]
          },
          range_or_options: 'yes,no',
          note: null,
          is_caption: false,
          is_stub: false,
          page_direction: null
        }
      ],
      register: null,
      range_or_options: null,
      note: null,
      is_caption: false,
      is_stub: false,
      page_direction: null
    }
  };
}

function createVisibilityReconciliationPayload() {
  return {
    ok: true,
    error: null,
    source_path: 'test',
    dashboard_sync_map: {
      '1': 'advanced_enabled'
    },
    root: {
      path: '',
      title: 'Root',
      display_label: 'Root',
      raw_text: 'Root',
      kind: 'root',
      children: [
        {
          path: '1',
          title: 'Advanced Enabled',
          display_label: 'Advanced Enabled',
          raw_text: 'Advanced Enabled',
          kind: 'leaf',
          children: [],
          register: null,
          dashboard_sync: 'advanced_enabled',
          range_or_options: null,
          note: null,
          is_caption: false,
          is_stub: false,
          page_direction: null
        },
        {
          path: '2',
          title: 'Advanced',
          display_label: 'Advanced',
          raw_text: 'Advanced',
          kind: 'menu',
          children: [
            {
              path: '2.1',
              title: 'Gain',
              display_label: 'Gain',
              raw_text: 'Gain (A,1,R/W)',
              kind: 'leaf',
              children: [],
              register: {
                family: 'A',
                index: 1,
                access: 'R/W'
              },
              range_or_options: '0...10',
              note: null,
              is_caption: false,
              is_stub: false,
              page_direction: null
            }
          ],
          register: null,
          visible_if: {
            path: '1',
            operator: 'equals',
            values: [1]
          },
          range_or_options: null,
          note: null,
          is_caption: false,
          is_stub: false,
          page_direction: null
        }
      ],
      register: null,
      range_or_options: null,
      note: null,
      is_caption: false,
      is_stub: false,
      page_direction: null
    }
  };
}

function createNoteArrayPayload() {
  return {
    ok: true,
    error: null,
    source_path: 'test',
    dashboard_sync_map: {},
    root: {
      path: '',
      title: 'Root',
      display_label: 'Root',
      raw_text: 'Root',
      kind: 'root',
      children: [
        {
          path: '1',
          title: 'Commented Field',
          display_label: 'Commented Field',
          raw_text: 'Commented Field',
          kind: 'leaf',
          children: [],
          register: null,
          range_or_options: null,
          note: ['First line.', 'Second line.'],
          is_caption: false,
          is_stub: false,
          page_direction: null
        }
      ],
      register: null,
      range_or_options: null,
      note: null,
      is_caption: false,
      is_stub: false,
      page_direction: null
    }
  };
}

test('dashboard refresh re-fetches visible remote leaves in the active menu', async () => {
  const harness = createMenuWidgetHarness();
  const { widget, fetchCalls } = harness;
  widget.init();
  widget.__testing.setCurrentMenuPath('3.2.2');

  harness.clearFetchCalls();

  await widget.handleDashboardRefresh({ ok: true, info: {}, alarms: {} });

  assert.ok(
    fetchCalls.some((url) => url.includes('path=3.2.2.2')),
    `expected visible leaf 3.2.2.2 to be refreshed, got: ${fetchCalls.join(', ')}`
  );
});

test('dashboard refresh does not fetch leaves hidden by visible_if rules', async () => {
  const harness = createMenuWidgetHarness({
    fetchImpl: async (url) => {
      const parsed = new URL(String(url), 'http://localhost/');
      const pathValue = parsed.searchParams.get('path');
      return {
        json: async () => ({
          ok: true,
          path: pathValue,
          value: pathValue === '3.2.1.1' ? 0 : 0
        })
      };
    }
  });
  const { widget, fetchCalls } = harness;
  widget.init();
  widget.__testing.setCurrentMenuPath('3.2.2');

  harness.clearFetchCalls();

  await widget.handleDashboardRefresh({ ok: true, info: {}, alarms: {} });

  assert.ok(
    !widget.__testing.getCurrentMenuChildPaths().includes('3.2.2.6'),
    'expected hidden dependent leaves to stay hidden'
  );
  assert.ok(
    !fetchCalls.some((url) => url.includes('path=3.2.2.6')),
    `expected hidden leaf 3.2.2.6 not to be refreshed, got: ${fetchCalls.join(', ')}`
  );
});

test('dashboard refresh syncs dashboard-backed values into the menu cache', async () => {
  const harness = createMenuWidgetHarness();
  const { widget, fetchCalls } = harness;
  widget.init();
  widget.__testing.setCurrentMenuPath('3.2');

  harness.clearFetchCalls();

  await widget.handleDashboardRefresh({
    ok: true,
    last_setpoint_c: 28.5,
    info: { conductivity: 321 },
    alarms: {}
  });

  assert.equal(widget.__testing.getStoredValue('2.1'), 28.5);
  assert.equal(widget.__testing.getStoredValue('4.6'), 321);
  assert.ok(
    !fetchCalls.some((url) => url.includes('path=4.6')),
    `expected dashboard-backed path 4.6 to sync from payload without a direct fetch, got: ${fetchCalls.join(', ')}`
  );
});

test('restores menu location from session storage after reinitialization', () => {
  const sessionStorageStore = new Map();
  let harness = createMenuWidgetHarness({ sessionStorageStore });
  let { widget } = harness;
  widget.init();

  widget.__testing.navigateToMenu('3.2');
  widget.__testing.moveMenuSelection(1);

  const storedLocation = JSON.parse(sessionStorageStore.get('carel-menu-location'));
  assert.equal(storedLocation.path, '3.2');
  assert.equal(storedLocation.selectedIndex, 1);

  harness = createMenuWidgetHarness({ sessionStorageStore });
  widget = harness.widget;
  widget.init();

  assert.equal(widget.__testing.getCurrentMenuPath(), '3.2');
  assert.equal(widget.__testing.getSelectedIndex(), 1);
});

test('ignores stored menu location when the path is hidden', () => {
  const sessionStorageStore = new Map([
    ['carel-menu-location', JSON.stringify({ path: '2', selectedIndex: 0 })]
  ]);
  const harness = createMenuWidgetHarness({
    payload: createVisibilityReconciliationPayload(),
    sessionStorageStore
  });
  const { widget } = harness;

  widget.init();

  assert.equal(widget.__testing.getCurrentMenuPath(), '');
  assert.ok(
    !widget.__testing.getCurrentMenuChildPaths().includes('2'),
    'expected hidden stored menu path not to be restored'
  );
});

test('rule-driver refresh makes dependent leaves visible when the driver changes', async () => {
  const harness = createMenuWidgetHarness({
    fetchImpl: async (url) => {
      const parsed = new URL(String(url), 'http://localhost/');
      const pathValue = parsed.searchParams.get('path');
      const valueByPath = {
        '3.2.1.1': 2,
        '3.2.1.5': false
      };
      return {
        json: async () => ({
          ok: true,
          path: pathValue,
          value: valueByPath[pathValue] ?? 0
        })
      };
    }
  });
  const { widget } = harness;
  widget.init();
  widget.__testing.setCurrentMenuPath('3.2.2');

  await widget.handleDashboardRefresh({ ok: true, info: {}, alarms: {} });

  const childPaths = widget.__testing.getCurrentMenuChildPaths();
  assert.ok(childPaths.includes('3.2.2.6'));
  assert.ok(childPaths.includes('3.2.2.7'));
  assert.ok(childPaths.includes('3.2.2.8'));
});

test('navigating away cancels the remaining refresh queue for the previous menu', async () => {
  const firstLeafDeferred = createDeferred();
  let holdPreviousMenuFetch = true;
  const harness = createMenuWidgetHarness({
    fetchImpl: async (url) => {
      const parsed = new URL(String(url), 'http://localhost/');
      const pathValue = parsed.searchParams.get('path');
      if (pathValue === '3.2.2.2' && holdPreviousMenuFetch) {
        return {
          json: async () => {
            await firstLeafDeferred.promise;
            return {
              ok: true,
              path: pathValue,
              value: 10
            };
          }
        };
      }
      return buildMenuValueResponse(pathValue, 0);
    }
  });
  const { widget, fetchCalls } = harness;
  widget.init();
  await flushAsyncWork();
  await flushAsyncWork();

  harness.clearFetchCalls();

  widget.__testing.navigateToMenu('3.2.2');
  assert.ok(
    fetchCalls.some((url) => url.includes('path=3.2.2.2')),
    `expected first previous-menu leaf fetch to start, got: ${fetchCalls.join(', ')}`
  );

  widget.__testing.navigateToMenu('3.2.1');
  assert.equal(widget.__testing.getCurrentMenuPath(), '3.2.1');
  assert.ok(
    fetchCalls.some((url) => url.includes('path=3.2.1.1')),
    `expected new menu fetches to start after navigation, got: ${fetchCalls.join(', ')}`
  );

  holdPreviousMenuFetch = false;
  firstLeafDeferred.resolve();
  await flushAsyncWork();
  await flushAsyncWork();

  assert.equal(widget.__testing.getCurrentMenuPath(), '3.2.1');
  assert.ok(
    !fetchCalls.some((url) => url.includes('path=3.2.2.3')),
    `expected remaining old-menu queue to stop after navigation, got: ${fetchCalls.join(', ')}`
  );
});

test('backend resolved editor metadata overrides local editor inference after refresh', async () => {
  const resolvedEditor = {
    type: 'enum',
    options: [
      { value: 0, label: 'Disabled' },
      { value: 1, label: 'Enabled' }
    ],
    step: null,
    scale: 1,
    limits: { min: 0, max: 1 },
    editable: true,
    writable: true,
    modbus_backed: true
  };
  const harness = createMenuWidgetHarness({
    fetchImpl: async (url) => {
      const parsed = new URL(String(url), 'http://localhost/');
      const pathValue = parsed.searchParams.get('path');
      if (pathValue === '3.2.2.2') {
        return buildMenuValueResponse(pathValue, 1, { resolved_editor: resolvedEditor });
      }
      return buildMenuValueResponse(pathValue, 0);
    }
  });
  const { widget } = harness;
  widget.init();
  widget.__testing.setCurrentMenuPath('3.2.2');

  await widget.handleDashboardRefresh({ ok: true, info: {}, alarms: {} });

  assert.deepEqual(JSON.parse(JSON.stringify(widget.__testing.getNode('3.2.2.2').resolved_editor)), resolvedEditor);
  const leafEditor = JSON.parse(JSON.stringify(widget.__testing.getLeafEditor('3.2.2.2')));
  assert.equal(leafEditor.type, 'enum');
  assert.deepEqual(leafEditor.options, resolvedEditor.options);
  assert.equal(leafEditor.step, null);
  assert.equal(leafEditor.scale, 1);
  assert.deepEqual(leafEditor.limits, { min: 0, max: 1 });
});

test('dashboard refresh reconciles the current menu when it becomes hidden', async () => {
  const harness = createMenuWidgetHarness({
    payload: createVisibilityReconciliationPayload()
  });
  const { widget } = harness;
  widget.init();

  await widget.handleDashboardRefresh({ ok: true, advanced_enabled: 1, info: {}, alarms: {} });
  assert.ok(widget.__testing.getCurrentMenuChildPaths().includes('2'));

  widget.__testing.setCurrentMenuPath('2');
  assert.equal(widget.__testing.getCurrentMenuPath(), '2');

  await widget.handleDashboardRefresh({ ok: true, advanced_enabled: 0, info: {}, alarms: {} });

  assert.equal(widget.__testing.getCurrentMenuPath(), '');
  assert.ok(
    !widget.__testing.getCurrentMenuChildPaths().includes('2'),
    'expected hidden menu to disappear after rule-driver refresh'
  );
});

test('measure-unit rule refresh updates runtime units and decorated range hints', async () => {
  const harness = createMenuWidgetHarness({
    fetchImpl: async (url) => {
      const parsed = new URL(String(url), 'http://localhost/');
      const pathValue = parsed.searchParams.get('path');
      const valueByPath = {
        '3.2.1.1': 0,
        '3.2.1.5': true
      };
      return buildMenuValueResponse(pathValue, valueByPath[pathValue] ?? 0);
    }
  });
  const { widget } = harness;
  widget.init();
  widget.__testing.setCurrentMenuPath('2');

  await widget.handleDashboardRefresh({ ok: true, info: {}, alarms: {} });

  assert.equal(widget.__testing.getNode('2.1').display_unit, '°F');
  assert.equal(widget.__testing.getNode('2.4').display_range_or_options, '2..19.9 °F');
});

test('renders array notes as multiline detail text', () => {
  const harness = createMenuWidgetHarness({
    payload: createNoteArrayPayload()
  });
  const { widget, document } = harness;

  widget.init();

  const detail = document.getElementById('menuDetail');
  const noteLine = detail.children.find(
    (child) => child.className === 'menu-detail-note' && child.textContent.startsWith('Note:')
  );

  assert.ok(noteLine, 'expected menu detail to include a note line');
  assert.equal(noteLine.textContent, 'Note: First line.\nSecond line.');
});

test('touch long press opens a menu item like double click', async () => {
  const harness = createMenuWidgetHarness({
    payload: createLongPressPayload()
  });
  const { widget, document } = harness;

  widget.init({ longPressDurationMs: 100 });

  const serviceLine = document.getElementById('menuScreen').children[1];
  dispatchElementEvent(serviceLine, 'pointerdown');
  await sleep(130);

  assert.equal(widget.__testing.getCurrentMenuPath(), '1');
});

test('touch long press opens the editor for editable leaves', async () => {
  const harness = createMenuWidgetHarness({
    payload: createLongPressPayload()
  });
  const { widget, document } = harness;

  widget.init({ longPressDurationMs: 100 });

  const editableLine = document.getElementById('menuScreen').children[2];
  dispatchElementEvent(editableLine, 'pointerdown');
  await sleep(130);

  assert.equal(document.getElementById('menuEditModalTitle').textContent, 'Edit Editable Leaf');
});
