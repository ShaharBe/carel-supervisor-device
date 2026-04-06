const test = require('node:test');
const assert = require('node:assert/strict');
const { createMenuWidgetHarness } = require('./menu_widget_harness');

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
