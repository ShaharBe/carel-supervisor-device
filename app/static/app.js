  let rtcModalOpen = false;
  let setpointModalOpen = false;
  let maxProductionModalOpen = false;
  let propBandModalOpen = false;
  let lastRtcIsoLocal = null;
  let lastSetpointC = null;
  let lastMaxProductionPct = null;
  let lastPropBandC = null;
  const humidifierStatusMap = {
    0: 'On duty',
    1: 'Alarm(s) present',
    2: 'Disabled via network',
    3: 'Disabled by timer',
    4: 'Disabled by remote on/off',
    5: 'Disabled by keyboard',
    6: 'Manual control',
    7: 'No demand'
  };
  let lastAlarmState = null;
  let clearAlarmsBusy = false;

  function browserDateTimeLocalValue() {
    const now = new Date();
    const local = new Date(now.getTime() - now.getTimezoneOffset() * 60000);
    return local.toISOString().slice(0, 16);
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

  function openSetpointModal() {
    if (lastSetpointC === null || lastSetpointC === undefined) {
      return;
    }

    setpointModalOpen = true;
    document.getElementById('setpointModalBackdrop').classList.add('open');
    document.getElementById('setpointModalBackdrop').setAttribute('aria-hidden', 'false');
    document.getElementById('setpointModalStatus').textContent = '';
    document.getElementById('setpointModalStatus').className = 'modal-status muted';
    const input = document.getElementById('setpointInput');
    input.value = lastSetpointC.toFixed(1);
    input.focus();
    input.select();
  }

  function closeSetpointModal() {
    setpointModalOpen = false;
    document.getElementById('setpointModalBackdrop').classList.remove('open');
    document.getElementById('setpointModalBackdrop').setAttribute('aria-hidden', 'true');
  }

  function openMaxProductionModal() {
    if (lastMaxProductionPct === null || lastMaxProductionPct === undefined) {
      return;
    }

    maxProductionModalOpen = true;
    document.getElementById('maxProductionModalBackdrop').classList.add('open');
    document.getElementById('maxProductionModalBackdrop').setAttribute('aria-hidden', 'false');
    document.getElementById('maxProductionModalStatus').textContent = '';
    document.getElementById('maxProductionModalStatus').className = 'modal-status muted';
    const input = document.getElementById('maxProductionInput');
    input.value = lastMaxProductionPct.toFixed(1);
    input.focus();
    input.select();
  }

  function closeMaxProductionModal() {
    maxProductionModalOpen = false;
    document.getElementById('maxProductionModalBackdrop').classList.remove('open');
    document.getElementById('maxProductionModalBackdrop').setAttribute('aria-hidden', 'true');
  }

  function openPropBandModal() {
    if (lastPropBandC === null || lastPropBandC === undefined) {
      return;
    }

    propBandModalOpen = true;
    document.getElementById('propBandModalBackdrop').classList.add('open');
    document.getElementById('propBandModalBackdrop').setAttribute('aria-hidden', 'false');
    document.getElementById('propBandModalStatus').textContent = '';
    document.getElementById('propBandModalStatus').className = 'modal-status muted';
    const input = document.getElementById('propBandInput');
    input.value = lastPropBandC.toFixed(1);
    input.focus();
    input.select();
  }

  function closePropBandModal() {
    propBandModalOpen = false;
    document.getElementById('propBandModalBackdrop').classList.remove('open');
    document.getElementById('propBandModalBackdrop').setAttribute('aria-hidden', 'true');
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

  async function refresh() {
    try {
      const r = await fetch('api/temp');
      const j = await r.json();

      lastRtcIsoLocal = j.device_time_iso_local || lastRtcIsoLocal;

      if (j.ok) {
        document.getElementById('temp').textContent = j.temp_c.toFixed(1) + ' \u00B0C';
        document.getElementById('status').textContent = 'OK';
        document.getElementById('status').className = 'top-value ok';
        document.getElementById('status').title = 'Latest Modbus poll succeeded.';
        setModbusIndicator('status-dot-live');
      } else {
        document.getElementById('temp').textContent = '\u2014';
        document.getElementById('status').textContent = 'Error';
        document.getElementById('status').className = 'top-value err';
        document.getElementById('status').title = j.error || 'No data';
        setModbusIndicator('status-dot-dead');
      }

      if (j.device_time_display) {
        document.getElementById('deviceTime').textContent = j.device_time_display;
        if (!rtcModalOpen && j.device_time_iso_local) {
          document.getElementById('rtcInput').value = j.device_time_iso_local;
        }
      } else {
        document.getElementById('deviceTime').textContent = '\u2014';
      }

      const hasSetpoint = j.last_setpoint_c !== null && j.last_setpoint_c !== undefined;
      lastSetpointC = hasSetpoint ? j.last_setpoint_c : null;
      document.getElementById('editSetpointBtn').disabled = !hasSetpoint;
      if (hasSetpoint) {
        document.getElementById('lsp').textContent = j.last_setpoint_c.toFixed(1) + ' \u00B0C';
      } else {
        document.getElementById('lsp').textContent = '\u2014';
      }

      const hasMaxProduction = j.max_production_pct !== null && j.max_production_pct !== undefined;
      lastMaxProductionPct = hasMaxProduction ? j.max_production_pct : null;
      document.getElementById('editMaxProductionBtn').disabled = !hasMaxProduction;
      if (hasMaxProduction) {
        document.getElementById('maxProductionValue').textContent = j.max_production_pct.toFixed(1) + ' %';
      } else {
        document.getElementById('maxProductionValue').textContent = '\u2014';
      }

      const hasPropBand = j.prop_band_c !== null && j.prop_band_c !== undefined;
      lastPropBandC = hasPropBand ? j.prop_band_c : null;
      document.getElementById('editPropBandBtn').disabled = !hasPropBand;
      if (hasPropBand) {
        document.getElementById('propBandValue').textContent = j.prop_band_c.toFixed(1) + ' \u00B0C';
      } else {
        document.getElementById('propBandValue').textContent = '\u2014';
      }

      const humidifierStatus = j.info?.humidifier_status;
      document.getElementById('humidifierStatus').textContent =
        humidifierStatusMap[humidifierStatus] ?? humidifierStatus ?? '\u2014';
      const humidifierToggleBtn = document.getElementById('humidifierToggleBtn');
      const humidifierNetworkEnabled = j.info?.humidifier_network_enabled;
      if (humidifierNetworkEnabled === true) {
        humidifierToggleBtn.textContent = 'Off';
        humidifierToggleBtn.disabled = false;
      } else if (humidifierNetworkEnabled === false) {
        humidifierToggleBtn.textContent = 'On';
        humidifierToggleBtn.disabled = false;
      } else {
        humidifierToggleBtn.textContent = '\u2014';
        humidifierToggleBtn.disabled = true;
      }

      // Info accordion
      if (j.info) {
        const phaseMap = {0:'Not active', 1:'Softstart', 2:'Start', 3:'Steady state', 4:'Reduced', 5:'Delayed stop', 6:'Full flush', 7:'Fast Start', 8:'Fast Start (foam)', 9:'Fast Start (heating)'};
        const statusMap = {0:'No production', 1:'Start evap', 2:'Water fill', 3:'Producing', 4:'Drain (deciding)', 5:'Drain (pump)', 6:'Drain (closing)', 7:'Blocked', 8:'Inactivity drain', 9:'Flushing', 10:'Manual drain', 11:'No supply water', 12:'Periodic drain'};
        const voltMap = {0:'200V', 1:'208V', 2:'230V', 3:'400V', 4:'460V', 5:'575V'};

        document.getElementById('infoHumStatus').textContent = humidifierStatusMap[j.info.humidifier_status] ?? j.info.humidifier_status ?? '\u2014';
        document.getElementById('infoConductivity').textContent = j.info.conductivity ?? '\u2014';
        document.getElementById('infoCyl1Phase').textContent = phaseMap[j.info.cyl1_phase] ?? j.info.cyl1_phase ?? '\u2014';
        document.getElementById('infoCyl1Status').textContent = statusMap[j.info.cyl1_status] ?? j.info.cyl1_status ?? '\u2014';
        document.getElementById('infoCyl2Phase').textContent = phaseMap[j.info.cyl2_phase] ?? j.info.cyl2_phase ?? '\u2014';
        document.getElementById('infoCyl2Status').textContent = statusMap[j.info.cyl2_status] ?? j.info.cyl2_status ?? '\u2014';
        document.getElementById('infoCyl1Hours').textContent = j.info.cyl1_hours ?? '\u2014';
        document.getElementById('infoCyl2Hours').textContent = j.info.cyl2_hours ?? '\u2014';
        document.getElementById('infoVoltage').textContent = voltMap[j.info.voltage_type] ?? j.info.voltage_type ?? '\u2014';
        document.getElementById('infoError').textContent = j.info.error || '';
        document.getElementById('infoError').className = j.info.error ? 'muted err' : 'muted';
        // Drain button
        const drainBtn = document.getElementById('drainCyl1Btn');
        if (j.info.cyl1_drain_on === true) {
          drainBtn.textContent = 'ON';
          drainBtn.style.background = '#ffcccc';
        } else if (j.info.cyl1_drain_on === false) {
          drainBtn.textContent = 'OFF';
          drainBtn.style.background = '';
        } else {
          drainBtn.textContent = '\u2014';
          drainBtn.style.background = '';
        }
      }

      renderAlarms(j.alarms);
    } catch (e) {
      document.getElementById('status').textContent = 'UI error';
      document.getElementById('status').className = 'top-value err';
      document.getElementById('status').title = String(e);
      document.getElementById('temp').textContent = '\u2014';
      document.getElementById('deviceTime').textContent = '\u2014';
      document.getElementById('maxProductionValue').textContent = '\u2014';
      document.getElementById('propBandValue').textContent = '\u2014';
      document.getElementById('editMaxProductionBtn').disabled = true;
      document.getElementById('editPropBandBtn').disabled = true;
      document.getElementById('humidifierStatus').textContent = '\u2014';
      document.getElementById('humidifierToggleBtn').textContent = '\u2014';
      document.getElementById('humidifierToggleBtn').disabled = true;
      setModbusIndicator('status-dot-dead');
      setAlarmBadge('alarm-pill-neutral', 'UI error');
      document.getElementById('alarmsEmpty').textContent = 'Unable to render alarms.';
      document.getElementById('alarmsHint').textContent = String(e);
      document.getElementById('alarmsHint').className = 'alarm-hint err';
    }
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

    const r = await fetch('api/device-datetime', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ datetime_local: value })
    });
    const j = await r.json();
    if (!j.ok) {
      modalStatus.textContent = 'Write failed: ' + (j.error || 'unknown');
      modalStatus.className = 'modal-status err';
      return;
    }

    lastRtcIsoLocal = j.device_time_iso_local || value;
    closeRtcModal();
    await refresh();
  }

  async function saveSetpoint() {
    const input = document.getElementById('setpointInput');
    const modalStatus = document.getElementById('setpointModalStatus');
    const saveBtn = document.getElementById('saveSetpointBtn');
    const cancelBtn = document.getElementById('cancelSetpointBtn');
    const v = Number(input.value);
    if (!Number.isFinite(v)) {
      modalStatus.textContent = 'Enter a valid setpoint (\u00B0C).';
      modalStatus.className = 'modal-status err';
      return;
    }

    modalStatus.textContent = 'Saving...';
    modalStatus.className = 'modal-status muted';
    input.disabled = true;
    saveBtn.disabled = true;
    cancelBtn.disabled = true;

    try {
      const r = await fetch('api/setpoint', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ temp_c: v })
      });
      const j = await r.json();
      if (!j.ok) {
        modalStatus.textContent = 'Write failed: ' + (j.error || 'unknown');
        modalStatus.className = 'modal-status err';
        input.focus();
        input.select();
        return;
      }
    } catch (e) {
      modalStatus.textContent = 'Write failed: ' + e;
      modalStatus.className = 'modal-status err';
      input.focus();
      input.select();
      return;
    } finally {
      input.disabled = false;
      saveBtn.disabled = false;
      cancelBtn.disabled = false;
    }

    closeSetpointModal();
    await refresh();
  }

  async function saveMaxProduction() {
    const input = document.getElementById('maxProductionInput');
    const modalStatus = document.getElementById('maxProductionModalStatus');
    const saveBtn = document.getElementById('saveMaxProductionBtn');
    const cancelBtn = document.getElementById('cancelMaxProductionBtn');
    const v = Number(input.value);
    if (!Number.isFinite(v)) {
      modalStatus.textContent = 'Enter a valid maximum production (%).';
      modalStatus.className = 'modal-status err';
      return;
    }

    modalStatus.textContent = 'Saving...';
    modalStatus.className = 'modal-status muted';
    input.disabled = true;
    saveBtn.disabled = true;
    cancelBtn.disabled = true;

    try {
      const r = await fetch('api/max-production', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ value_pct: v })
      });
      const j = await r.json();
      if (!j.ok) {
        modalStatus.textContent = 'Write failed: ' + (j.error || 'unknown');
        modalStatus.className = 'modal-status err';
        input.focus();
        input.select();
        return;
      }
    } catch (e) {
      modalStatus.textContent = 'Write failed: ' + e;
      modalStatus.className = 'modal-status err';
      input.focus();
      input.select();
      return;
    } finally {
      input.disabled = false;
      saveBtn.disabled = false;
      cancelBtn.disabled = false;
    }

    closeMaxProductionModal();
    await refresh();
  }

  async function savePropBand() {
    const input = document.getElementById('propBandInput');
    const modalStatus = document.getElementById('propBandModalStatus');
    const saveBtn = document.getElementById('savePropBandBtn');
    const cancelBtn = document.getElementById('cancelPropBandBtn');
    const v = Number(input.value);
    if (!Number.isFinite(v)) {
      modalStatus.textContent = 'Enter a valid prop. band (\u00B0C).';
      modalStatus.className = 'modal-status err';
      return;
    }

    modalStatus.textContent = 'Saving...';
    modalStatus.className = 'modal-status muted';
    input.disabled = true;
    saveBtn.disabled = true;
    cancelBtn.disabled = true;

    try {
      const r = await fetch('api/prop-band', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ value_c: v })
      });
      const j = await r.json();
      if (!j.ok) {
        modalStatus.textContent = 'Write failed: ' + (j.error || 'unknown');
        modalStatus.className = 'modal-status err';
        input.focus();
        input.select();
        return;
      }
    } catch (e) {
      modalStatus.textContent = 'Write failed: ' + e;
      modalStatus.className = 'modal-status err';
      input.focus();
      input.select();
      return;
    } finally {
      input.disabled = false;
      saveBtn.disabled = false;
      cancelBtn.disabled = false;
    }

    closePropBandModal();
    await refresh();
  }

  async function clearAlarms() {
    clearAlarmsBusy = true;
    syncClearAlarmsButton();

    try {
      const r = await fetch('api/alarms-reset', { method: 'POST' });
      const j = await r.json();
      if (!j.ok) {
        alert('Alarm reset failed: ' + (j.error || 'unknown'));
        return;
      }
    } catch (e) {
      alert('Alarm reset failed: ' + e);
    } finally {
      clearAlarmsBusy = false;
      await refresh();
    }
  }

  async function toggleHumidifier() {
    const btn = document.getElementById('humidifierToggleBtn');
    btn.disabled = true;
    try {
      const r = await fetch('api/humidifier-toggle', { method: 'POST' });
      const j = await r.json();
      if (!j.ok) {
        alert('Humidifier toggle failed: ' + (j.error || 'unknown'));
      }
    } catch (e) {
      alert('Humidifier toggle failed: ' + e);
    } finally {
      await refresh();
    }
  }

  async function rebootDevice() {
    const shouldReboot = window.confirm('Are you sure you want to reboot the device?');
    if (!shouldReboot) return;

    const rebootBtn = document.getElementById('rebootBtn');
    const systemStatus = document.getElementById('systemStatus');
    rebootBtn.disabled = true;
    systemStatus.textContent = 'Sending reboot command...';
    systemStatus.className = 'muted';

    try {
      const r = await fetch('api/reboot', { method: 'POST' });
      const j = await r.json();
      if (!j.ok) {
        systemStatus.textContent = j.error || 'Reboot failed.';
        systemStatus.className = 'err';
        return;
      }
      systemStatus.textContent = j.message || 'Reboot command sent.';
      systemStatus.className = 'muted';
    } catch (e) {
      systemStatus.textContent = 'Reboot failed: ' + e;
      systemStatus.className = 'err';
    } finally {
      rebootBtn.disabled = false;
    }
  }

  document.getElementById('editSetpointBtn').addEventListener('click', openSetpointModal);
  document.getElementById('saveSetpointBtn').addEventListener('click', saveSetpoint);
  document.getElementById('cancelSetpointBtn').addEventListener('click', closeSetpointModal);
  document.getElementById('setpointInput').addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      saveSetpoint();
    } else if (event.key === 'Escape') {
      event.preventDefault();
      closeSetpointModal();
    }
  });
  document.getElementById('editMaxProductionBtn').addEventListener('click', openMaxProductionModal);
  document.getElementById('saveMaxProductionBtn').addEventListener('click', saveMaxProduction);
  document.getElementById('cancelMaxProductionBtn').addEventListener('click', closeMaxProductionModal);
  document.getElementById('maxProductionInput').addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      saveMaxProduction();
    } else if (event.key === 'Escape') {
      event.preventDefault();
      closeMaxProductionModal();
    }
  });
  document.getElementById('editPropBandBtn').addEventListener('click', openPropBandModal);
  document.getElementById('savePropBandBtn').addEventListener('click', savePropBand);
  document.getElementById('cancelPropBandBtn').addEventListener('click', closePropBandModal);
  document.getElementById('propBandInput').addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      savePropBand();
    } else if (event.key === 'Escape') {
      event.preventDefault();
      closePropBandModal();
    }
  });
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
  document.getElementById('setpointModalBackdrop').addEventListener('click', (event) => {
    if (event.target.id === 'setpointModalBackdrop') {
      closeSetpointModal();
    }
  });
  document.getElementById('maxProductionModalBackdrop').addEventListener('click', (event) => {
    if (event.target.id === 'maxProductionModalBackdrop') {
      closeMaxProductionModal();
    }
  });
  document.getElementById('propBandModalBackdrop').addEventListener('click', (event) => {
    if (event.target.id === 'propBandModalBackdrop') {
      closePropBandModal();
    }
  });
  document.getElementById('humidifierToggleBtn').addEventListener('click', toggleHumidifier);
  document.getElementById('drainCyl1Btn').addEventListener('click', async () => {
    const btn = document.getElementById('drainCyl1Btn');
    btn.disabled = true;
    try {
      const r = await fetch('api/cyl1-drain', { method: 'POST' });
      const j = await r.json();
      if (!j.ok) alert('Toggle failed: ' + (j.error || 'unknown'));
    } finally {
      btn.disabled = false;
      await refresh();
    }
  });
  document.getElementById('rebootBtn').addEventListener('click', rebootDevice);
  document.getElementById('clearAlarmsBtn').addEventListener('click', clearAlarms);

  refresh();
  setInterval(refresh, 1000);
