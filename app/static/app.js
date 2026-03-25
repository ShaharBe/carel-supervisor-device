(() => {
  let menuWidget = window.CarelMenuWidget;

  let rtcModalOpen = false;
  let lastRtcIsoLocal = null;
  let lastAlarmState = null;
  let clearAlarmsBusy = false;
  let refreshInFlight = null;
  let refreshQueued = false;

  const humidifierStatusMap = {
    0: 'on duty',
    1: 'alarm(s) present',
    2: 'disabled via network',
    3: 'disabled by timer',
    4: 'disabled by remote on/off',
    5: 'disabled by keyboard',
    6: 'manual control',
    7: 'no demand'
  };

  const phaseMap = {
    0: 'Not active',
    1: 'Softstart',
    2: 'Start',
    3: 'Steady state',
    4: 'Reduced',
    5: 'Delayed stop',
    6: 'Full flush',
    7: 'Fast Start',
    8: 'Fast Start (foam)',
    9: 'Fast Start (heating)'
  };

  const statusMap = {
    0: 'No production',
    1: 'Start evap',
    2: 'Water fill',
    3: 'Producing',
    4: 'Drain (deciding)',
    5: 'Drain (pump)',
    6: 'Drain (closing)',
    7: 'Blocked',
    8: 'Inactivity drain',
    9: 'Flushing',
    10: 'Manual drain',
    11: 'No supply water',
    12: 'Periodic drain'
  };

  const voltMap = {
    0: '200V',
    1: '208V',
    2: '230V',
    3: '400V',
    4: '460V',
    5: '575V'
  };

  function browserDateTimeLocalValue() {
    const now = new Date();
    const local = new Date(now.getTime() - now.getTimezoneOffset() * 60000);
    return local.toISOString().slice(0, 16);
  }

  function menuWidgetScriptUrl() {
    const existing = document.querySelector('script[src*="menu-widget.js"]');
    return existing?.src || 'static/menu-widget.js';
  }

  function loadMenuWidgetScript() {
    return new Promise((resolve, reject) => {
      if (window.CarelMenuWidget) {
        resolve(window.CarelMenuWidget);
        return;
      }

      const script = document.createElement('script');
      script.src = menuWidgetScriptUrl();
      script.async = false;
      script.onload = () => resolve(window.CarelMenuWidget || null);
      script.onerror = () => reject(new Error('Unable to load menu-widget.js'));
      document.head.appendChild(script);
    });
  }

  function showMenuBootError(message) {
    const path = document.getElementById('menuWidgetPath');
    const state = document.getElementById('menuWidgetState');
    const screen = document.getElementById('menuScreen');
    const detail = document.getElementById('menuDetail');

    if (path) {
      path.textContent = 'Menu unavailable';
    }
    if (state) {
      state.textContent = 'Script error';
      state.className = 'menu-widget-state err';
    }
    if (screen) {
      screen.replaceChildren();
      screen.style.gridTemplateRows = '1fr';
      const line = document.createElement('div');
      line.className = 'menu-line-empty';
      line.textContent = message;
      screen.appendChild(line);
    }
    if (detail) {
      detail.textContent = 'The menu widget could not start.';
      detail.className = 'menu-detail err';
    }
  }

  function openRtcModal() {
    rtcModalOpen = true;
    document.getElementById('rtcModalBackdrop').classList.add('open');
    document.getElementById('rtcModalBackdrop').setAttribute('aria-hidden', 'false');
    document.getElementById('rtcModalStatus').textContent = '';
    document.getElementById('rtcModalStatus').className = 'modal-status muted';
    if (lastRtcIsoLocal) {
      document.getElementById('rtcInput').value = lastRtcIsoLocal;
    }
    document.getElementById('rtcInput').focus();
  }

  function closeRtcModal() {
    rtcModalOpen = false;
    document.getElementById('rtcModalBackdrop').classList.remove('open');
    document.getElementById('rtcModalBackdrop').setAttribute('aria-hidden', 'true');
  }

  function setAlarmBadge(mode, text) {
    const badge = document.getElementById('alarmsBadge');
    badge.textContent = text;
    badge.className = 'alarm-pill ' + mode;
  }

  function setModbusIndicator(state) {
    const dot = document.getElementById('modbusStatusDot');
    dot.className = 'status-dot ' + state;
  }

  function syncClearAlarmsButton() {
    const clearBtn = document.getElementById('clearAlarmsBtn');
    clearBtn.textContent = clearAlarmsBusy ? 'Clearing...' : 'Clear alarms';
    clearBtn.disabled = clearAlarmsBusy || !(lastAlarmState && lastAlarmState.has_active === true);
  }

  function renderAlarms(alarms) {
    lastAlarmState = alarms;
    const empty = document.getElementById('alarmsEmpty');
    const list = document.getElementById('alarmsList');
    const hint = document.getElementById('alarmsHint');
    list.replaceChildren();
    list.hidden = true;
    empty.hidden = false;
    hint.textContent = '';
    hint.className = 'alarm-hint muted';
    syncClearAlarmsButton();

    if (!alarms) {
      setAlarmBadge('alarm-pill-neutral', 'Unavailable');
      empty.textContent = 'Waiting for alarm status...';
      return;
    }

    if (alarms.error) {
      setAlarmBadge('alarm-pill-neutral', 'Read error');
      empty.textContent = 'Unable to load alarms right now.';
      hint.textContent = alarms.error;
      hint.className = 'alarm-hint err';
      return;
    }

    if (alarms.has_active === true) {
      setAlarmBadge('alarm-pill-active', 'Active');
      if (alarms.active.length > 0) {
        empty.hidden = true;
        list.hidden = false;
        alarms.active.forEach((alarm) => {
          const card = document.createElement('div');
          card.className = 'alarm-card';

          const description = document.createElement('div');
          description.className = 'alarm-description';
          description.textContent = alarm.description;

          card.append(description);
          list.appendChild(card);
        });
      } else if (alarms.skipped_active_count > 0) {
        empty.textContent = 'Alarm summary is active, but only intentionally skipped cylinder 2 alarm bits are set.';
      } else {
        empty.textContent = 'Alarm summary is active, but no monitored alarm bits are currently set.';
      }
    } else if (alarms.has_active === false) {
      setAlarmBadge('alarm-pill-clear', 'Clear');
      empty.textContent = 'No active alarms.';
    } else {
      setAlarmBadge('alarm-pill-neutral', 'Checking...');
      empty.textContent = 'Waiting for alarm status...';
    }

    if (alarms.skipped_active_count > 0) {
      const plural = alarms.skipped_active_count === 1 ? 'bit' : 'bits';
      hint.textContent =
        'Cylinder 2 alarms are intentionally skipped on this unit (' +
        alarms.skipped_active_count + ' skipped ' + plural + ' active).';
      hint.className = 'alarm-hint muted';
    } else {
      hint.className = 'alarm-hint muted';
    }
  }

  function renderInfo(info) {
    if (!info) {
      document.getElementById('topHumidifierStatus').textContent = '\u2014';
      return;
    }

    const humidifierStatusText =
      humidifierStatusMap[info.humidifier_status] ?? info.humidifier_status ?? '\u2014';

    document.getElementById('topHumidifierStatus').textContent = humidifierStatusText;
    document.getElementById('infoHumStatus').textContent = humidifierStatusText;
    document.getElementById('infoConductivity').textContent = info.conductivity ?? '\u2014';
    document.getElementById('infoCyl1Phase').textContent = phaseMap[info.cyl1_phase] ?? info.cyl1_phase ?? '\u2014';
    document.getElementById('infoCyl1Status').textContent = statusMap[info.cyl1_status] ?? info.cyl1_status ?? '\u2014';
    document.getElementById('infoCyl2Phase').textContent = phaseMap[info.cyl2_phase] ?? info.cyl2_phase ?? '\u2014';
    document.getElementById('infoCyl2Status').textContent = statusMap[info.cyl2_status] ?? info.cyl2_status ?? '\u2014';
    document.getElementById('infoCyl1Hours').textContent = info.cyl1_hours ?? '\u2014';
    document.getElementById('infoCyl2Hours').textContent = info.cyl2_hours ?? '\u2014';
    document.getElementById('infoVoltage').textContent = voltMap[info.voltage_type] ?? info.voltage_type ?? '\u2014';
    document.getElementById('infoError').textContent = info.error || '';
    document.getElementById('infoError').className = info.error ? 'muted err' : 'muted';

    const drainBtn = document.getElementById('drainCyl1Btn');
    if (info.cyl1_drain_on === true) {
      drainBtn.textContent = 'ON';
      drainBtn.style.background = '#ffcccc';
    } else if (info.cyl1_drain_on === false) {
      drainBtn.textContent = 'OFF';
      drainBtn.style.background = '';
    } else {
      drainBtn.textContent = '\u2014';
      drainBtn.style.background = '';
    }
  }

  async function performRefresh() {
    try {
      const response = await fetch('api/temp');
      const payload = await response.json();
      if (menuWidget?.handleDashboardRefresh) {
        await menuWidget.handleDashboardRefresh(payload);
      }

      lastRtcIsoLocal = payload.device_time_iso_local || lastRtcIsoLocal;

      if (payload.ok) {
        document.getElementById('temp').textContent = payload.temp_c.toFixed(1) + ' \u00B0C';
        document.getElementById('status').textContent = 'OK';
        document.getElementById('status').className = 'top-value ok';
        document.getElementById('status').title = 'Latest Modbus poll succeeded.';
        setModbusIndicator('status-dot-live');
      } else {
        document.getElementById('temp').textContent = '\u2014';
        document.getElementById('status').textContent = 'Error';
        document.getElementById('status').className = 'top-value err';
        document.getElementById('status').title = payload.error || 'No data';
        setModbusIndicator('status-dot-dead');
      }

      if (payload.device_time_display) {
        document.getElementById('deviceTime').textContent = payload.device_time_display;
        if (!rtcModalOpen && payload.device_time_iso_local) {
          document.getElementById('rtcInput').value = payload.device_time_iso_local;
        }
      } else {
        document.getElementById('deviceTime').textContent = '\u2014';
      }

      renderInfo(payload.info);
      renderAlarms(payload.alarms);
    } catch (error) {
      document.getElementById('status').textContent = 'UI error';
      document.getElementById('status').className = 'top-value err';
      document.getElementById('status').title = String(error);
      document.getElementById('temp').textContent = '\u2014';
      document.getElementById('deviceTime').textContent = '\u2014';
      document.getElementById('topHumidifierStatus').textContent = '\u2014';
      setModbusIndicator('status-dot-dead');
      setAlarmBadge('alarm-pill-neutral', 'UI error');
      document.getElementById('alarmsEmpty').textContent = 'Unable to render alarms.';
      document.getElementById('alarmsHint').textContent = String(error);
      document.getElementById('alarmsHint').className = 'alarm-hint err';
    }
  }

  function refresh() {
    if (refreshInFlight) {
      refreshQueued = true;
      return refreshInFlight;
    }

    refreshInFlight = (async () => {
      try {
        await performRefresh();
      } finally {
        refreshInFlight = null;
        if (refreshQueued) {
          refreshQueued = false;
          void refresh();
        }
      }
    })();

    return refreshInFlight;
  }

  async function saveRtc() {
    const input = document.getElementById('rtcInput');
    const modalStatus = document.getElementById('rtcModalStatus');
    const value = input.value;
    if (!value) {
      modalStatus.textContent = 'Pick a valid date/time first.';
      modalStatus.className = 'modal-status err';
      return;
    }

    modalStatus.textContent = 'Saving...';
    modalStatus.className = 'modal-status muted';

    const response = await fetch('api/device-datetime', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ datetime_local: value })
    });
    const payload = await response.json();
    if (!payload.ok) {
      modalStatus.textContent = 'Write failed: ' + (payload.error || 'unknown');
      modalStatus.className = 'modal-status err';
      return;
    }

    lastRtcIsoLocal = payload.device_time_iso_local || value;
    closeRtcModal();
    await refresh();
  }

  async function clearAlarms() {
    clearAlarmsBusy = true;
    syncClearAlarmsButton();

    try {
      const response = await fetch('api/alarms-reset', { method: 'POST' });
      const payload = await response.json();
      if (!payload.ok) {
        alert('Alarm reset failed: ' + (payload.error || 'unknown'));
        return;
      }
    } catch (error) {
      alert('Alarm reset failed: ' + error);
    } finally {
      clearAlarmsBusy = false;
      await refresh();
    }
  }

  async function rebootDevice() {
    const shouldReboot = window.confirm('Are you sure you want to reboot the device?');
    if (!shouldReboot) {
      return;
    }

    const rebootBtn = document.getElementById('rebootBtn');
    const systemStatus = document.getElementById('systemStatus');
    rebootBtn.disabled = true;
    systemStatus.textContent = 'Sending reboot command...';
    systemStatus.className = 'muted';

    try {
      const response = await fetch('api/reboot', { method: 'POST' });
      const payload = await response.json();
      if (!payload.ok) {
        systemStatus.textContent = payload.error || 'Reboot failed.';
        systemStatus.className = 'err';
        return;
      }
      systemStatus.textContent = payload.message || 'Reboot command sent.';
      systemStatus.className = 'muted';
    } catch (error) {
      systemStatus.textContent = 'Reboot failed: ' + error;
      systemStatus.className = 'err';
    } finally {
      rebootBtn.disabled = false;
    }
  }

  document.getElementById('editRtcBtn').addEventListener('click', openRtcModal);
  document.getElementById('cancelRtcBtn').addEventListener('click', closeRtcModal);
  document.getElementById('saveRtcBtn').addEventListener('click', saveRtc);
  document.getElementById('useBrowserTimeBtn').addEventListener('click', () => {
    document.getElementById('rtcInput').value = browserDateTimeLocalValue();
    document.getElementById('rtcModalStatus').textContent = 'Browser time loaded.';
    document.getElementById('rtcModalStatus').className = 'modal-status muted';
  });
  document.getElementById('rtcModalBackdrop').addEventListener('click', (event) => {
    if (event.target.id === 'rtcModalBackdrop') {
      closeRtcModal();
    }
  });
  document.getElementById('drainCyl1Btn').addEventListener('click', async () => {
    const button = document.getElementById('drainCyl1Btn');
    button.disabled = true;
    try {
      const response = await fetch('api/cyl1-drain', { method: 'POST' });
      const payload = await response.json();
      if (!payload.ok) {
        alert('Toggle failed: ' + (payload.error || 'unknown'));
      }
    } finally {
      button.disabled = false;
      await refresh();
    }
  });
  document.getElementById('rebootBtn').addEventListener('click', rebootDevice);
  document.getElementById('clearAlarmsBtn').addEventListener('click', clearAlarms);

  async function bootstrap() {
    if (!menuWidget) {
      try {
        menuWidget = await loadMenuWidgetScript();
      } catch (error) {
        showMenuBootError(String(error));
        throw error;
      }
    }

    if (!menuWidget) {
      const error = new Error('Menu widget module is unavailable.');
      showMenuBootError(error.message);
      throw error;
    }

    try {
      menuWidget.init({ refreshPage: refresh });
    } catch (error) {
      showMenuBootError(String(error));
      throw error;
    }

    await refresh();
    setInterval(refresh, 1000);
  }

  bootstrap();
})();
