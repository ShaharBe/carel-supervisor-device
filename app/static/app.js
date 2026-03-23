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
  let menuPayload = null;
  let menuIndex = new Map();
  let menuCurrentPath = '';
  let menuSelectedIndex = 0;
  const menuValueStore = new Map();
  let menuEditState = {
    open: false,
    nodePath: null,
    editor: null,
    draftValue: null
  };

  function browserDateTimeLocalValue() {
    const now = new Date();
    const local = new Date(now.getTime() - now.getTimezoneOffset() * 60000);
    return local.toISOString().slice(0, 16);
  }

  function parseMenuPayload() {
    try {
      const node = document.getElementById('displayMenuData');
      return JSON.parse(node.textContent);
    } catch (e) {
      return {
        ok: false,
        error: 'Unable to read menu definition from page: ' + e,
        root: { path: '', title: 'Root', display_label: 'Root', kind: 'root', children: [] }
      };
    }
  }

  function indexMenuTree(node, parentPath) {
    if (!node) {
      return;
    }

    node.parent_path = parentPath;
    menuIndex.set(node.path, node);
    (node.children || []).forEach((child) => indexMenuTree(child, node.path));
  }

  function initializeMenuWidget() {
    menuPayload = parseMenuPayload();
    menuIndex = new Map();
    indexMenuTree(menuPayload.root, null);
    menuCurrentPath = '';
    menuSelectedIndex = 0;
    renderMenuWidget();
  }

  function getMenuNode(path) {
    return menuIndex.get(path) || menuPayload.root;
  }

  function isMenuNodeVisible(node) {
    return node?.visible !== false;
  }

  function getVisibleMenuChildren(node) {
    return (node.children || []).filter((child) => isMenuNodeVisible(child));
  }

  function getCurrentMenuChildren() {
    return getVisibleMenuChildren(getMenuNode(menuCurrentPath));
  }

  function clampMenuSelection() {
    const children = getCurrentMenuChildren();
    if (children.length === 0) {
      menuSelectedIndex = 0;
      return;
    }

    if (menuSelectedIndex < 0) {
      menuSelectedIndex = children.length - 1;
    } else if (menuSelectedIndex >= children.length) {
      menuSelectedIndex = 0;
    }
  }

  function getSelectedMenuNode() {
    const children = getCurrentMenuChildren();
    if (children.length === 0) {
      return null;
    }
    clampMenuSelection();
    return children[menuSelectedIndex];
  }

  function menuLineHint(node) {
    if (node.kind === 'menu') {
      return '\u203A';
    }
    if (node.kind === 'page_link') {
      return node.page_direction === 'prev' ? '\u2190' : '\u2192';
    }
    if (node.kind === 'caption') {
      return '';
    }
    if (node.kind === 'stub') {
      return 'TBD';
    }
    return node.register ? node.register.access : 'R';
  }

  function buildMenuBreadcrumb(node) {
    const labels = [];
    let current = node;
    while (current && current.path !== '') {
      labels.unshift(current.display_label || current.title || current.path);
      current = current.parent_path === null ? null : getMenuNode(current.parent_path);
    }
    return labels.length > 0 ? labels.join(' > ') : 'Root';
  }

  function parseChoiceTokens(text) {
    if (!text) {
      return [];
    }

    const separator = text.includes(',') ? ',' : (text.includes('/') ? '/' : null);
    if (!separator) {
      return [];
    }

    return text
      .split(separator)
      .map((token) => token.trim().replace(/^"+|"+$/g, ''))
      .filter(Boolean);
  }

  function isNumericRangeHint(text) {
    return /-?\d+(?:\.\d+)?\s*(?:\.{2,3})\s*-?\d+(?:\.\d+)?/.test(text || '');
  }

  function inferNumericEditorType(node) {
    const family = node.register?.family;
    if (family !== 'A' && family !== 'I') {
      return null;
    }

    const hint = node.range_or_options || '';
    const label = node.display_label || '';
    if (/\d+\.\d+/.test(hint) || /\b(offset|band|setpoint|hyster)\b/i.test(label)) {
      return 'float';
    }

    return 'integer';
  }

  function normalizeEditorOptions(options) {
    return (options || []).map((option, index) => {
      if (option && typeof option === 'object') {
        return {
          value: option.value ?? index,
          label: option.label ?? String(option.value ?? index)
        };
      }

      return {
        value: index,
        label: String(option)
      };
    });
  }

  function getLeafEditor(node) {
    if (!node) {
      return null;
    }

    if (node.editor) {
      const explicitType = node.editor.type || 'enum';
      return {
        type: explicitType,
        options: normalizeEditorOptions(node.editor.options),
        currentValue: node.editor.current_value,
        step: node.editor.step || (explicitType === 'float' ? 'any' : '1')
      };
    }

    if (node.register?.family === 'D') {
      const labels = parseChoiceTokens(node.range_or_options);
      const optionLabels = labels.length >= 2 ? labels.slice(0, 2) : ['yes', 'no'];
      return {
        type: 'boolean',
        options: [
          { value: true, label: optionLabels[0] },
          { value: false, label: optionLabels[1] }
        ],
        currentValue: undefined,
        step: null
      };
    }

    const parsedChoices = parseChoiceTokens(node.range_or_options);
    if (parsedChoices.length >= 2 && !isNumericRangeHint(node.range_or_options)) {
      return {
        type: 'enum',
        options: parsedChoices.map((label, index) => ({ value: index, label })),
        currentValue: undefined,
        step: null
      };
    }

    const numericType = inferNumericEditorType(node);
    if (!numericType) {
      return null;
    }

    return {
      type: numericType,
      options: [],
      currentValue: undefined,
      step: numericType === 'float' ? 'any' : '1'
    };
  }

  function isMenuNodeEditable(node) {
    return !['menu', 'caption', 'page_link'].includes(node?.kind) && getLeafEditor(node) !== null;
  }

  function getDefaultNumericValue(node, editor) {
    const match = (node.range_or_options || '').match(/-?\d+(?:\.\d+)?/);
    if (match) {
      return editor.type === 'float' ? Number.parseFloat(match[0]) : Number.parseInt(match[0], 10);
    }

    return 0;
  }

  function getCurrentMenuValue(node, editor) {
    if (menuValueStore.has(node.path)) {
      return menuValueStore.get(node.path);
    }

    if (editor.currentValue !== undefined) {
      return editor.currentValue;
    }

    if (editor.type === 'boolean') {
      return editor.options[0]?.value ?? true;
    }

    if (editor.type === 'enum') {
      return editor.options[0]?.value ?? '';
    }

    return getDefaultNumericValue(node, editor);
  }

  function formatMenuValue(node, value, editorOverride) {
    const editor = editorOverride || getLeafEditor(node);
    if (!editor) {
      return 'Unavailable';
    }

    if (value === null || value === undefined || value === '') {
      return 'Not set';
    }

    if (editor.type === 'boolean' || editor.type === 'enum') {
      const match = editor.options.find((option) => String(option.value) === String(value));
      if (match) {
        return match.label;
      }
    }

    return String(value);
  }

  function menuDetailMeta(node) {
    const fragments = [];
    if (node.kind === 'menu') {
      const count = getVisibleMenuChildren(node).length;
      const itemLabel = count === 1 ? 'item' : 'items';
      fragments.push('Submenu with ' + count + ' ' + itemLabel);
    } else if (node.kind === 'caption') {
      fragments.push('Display-only caption');
    } else if (node.kind === 'page_link') {
      fragments.push('Page navigation item');
    } else if (node.kind === 'stub') {
      fragments.push('Defined, but still marked stub/TBD');
    } else {
      fragments.push('Leaf item');
    }

    if (node.register) {
      fragments.push(
        'Register ' + node.register.family + ',' + node.register.index + ' (' + node.register.access + ')'
      );
    }

    if (node.range_or_options) {
      fragments.push('Options/range: ' + node.range_or_options);
    }

    return fragments.join(' | ');
  }

  function renderMenuDetail(node) {
    const detail = document.getElementById('menuDetail');
    detail.replaceChildren();

    if (!node) {
      detail.textContent = 'This menu has no items.';
      detail.className = 'menu-detail muted';
      return;
    }

    detail.className = 'menu-detail';

    const title = document.createElement('div');
    title.className = 'menu-detail-title';
    title.textContent = node.display_label || node.title || 'Unnamed item';
    detail.appendChild(title);

    const meta = document.createElement('div');
    meta.className = 'menu-detail-meta';
    meta.textContent = menuDetailMeta(node);
    detail.appendChild(meta);

    const guidance = document.createElement('div');
    guidance.className = 'menu-detail-note';
    if (node.kind === 'menu') {
      guidance.textContent = 'Press Enter to open this submenu.';
    } else if (node.kind === 'page_link') {
      guidance.textContent =
        node.page_direction === 'prev'
          ? 'Press Enter to jump to the previous sibling page.'
          : 'Press Enter to jump to the next sibling page.';
    } else if (isMenuNodeEditable(node)) {
      guidance.textContent = 'Double-click to edit this value locally.';
    } else {
      guidance.textContent = 'This prototype only sketches navigation, so leaf actions are not wired yet.';
    }
    detail.appendChild(guidance);

    if (isMenuNodeEditable(node)) {
      const currentValue = document.createElement('div');
      currentValue.className = 'menu-detail-note';
      currentValue.textContent =
        'Current UI value: ' + formatMenuValue(node, getCurrentMenuValue(node, getLeafEditor(node)));
      detail.appendChild(currentValue);
    }

    if (node.note) {
      const note = document.createElement('div');
      note.className = 'menu-detail-note';
      note.textContent = 'Note: ' + node.note;
      detail.appendChild(note);
    }

    const raw = document.createElement('div');
    raw.className = 'menu-detail-raw';
    raw.textContent = 'Definition: ' + node.raw_text;
    detail.appendChild(raw);
  }

  function syncMenuControls(children) {
    const hasSelection = children.length > 0;
    document.getElementById('menuUpBtn').disabled = !hasSelection;
    document.getElementById('menuDownBtn').disabled = !hasSelection;
    document.getElementById('menuEnterBtn').disabled = !hasSelection;
    document.getElementById('menuBackBtn').disabled = menuCurrentPath === '';
    document.getElementById('menuHomeBtn').disabled = menuCurrentPath === '';
  }

  function findSelectableChildIndex(menuNode, preferredChildPath) {
    const visibleChildren = getVisibleMenuChildren(menuNode);
    if (visibleChildren.length === 0) {
      return 0;
    }

    if (!preferredChildPath) {
      return 0;
    }

    const directIndex = visibleChildren.findIndex((child) => child.path === preferredChildPath);
    if (directIndex !== -1) {
      return directIndex;
    }

    const allChildren = menuNode.children || [];
    const targetIndex = allChildren.findIndex((child) => child.path === preferredChildPath);
    if (targetIndex === -1) {
      return 0;
    }

    for (let index = targetIndex - 1; index >= 0; index -= 1) {
      if (isMenuNodeVisible(allChildren[index])) {
        return visibleChildren.findIndex((child) => child.path === allChildren[index].path);
      }
    }

    for (let index = targetIndex + 1; index < allChildren.length; index += 1) {
      if (isMenuNodeVisible(allChildren[index])) {
        return visibleChildren.findIndex((child) => child.path === allChildren[index].path);
      }
    }

    return 0;
  }

  function renderMenuWidget() {
    const screen = document.getElementById('menuScreen');
    const path = document.getElementById('menuWidgetPath');
    const state = document.getElementById('menuWidgetState');

    if (!menuPayload || !menuPayload.ok) {
      path.textContent = 'Menu unavailable';
      state.textContent = 'Read error';
      state.className = 'menu-widget-state err';
      screen.replaceChildren();
      screen.style.gridTemplateRows = '1fr';

      const empty = document.createElement('div');
      empty.className = 'menu-line-empty';
      empty.textContent = menuPayload?.error || 'Menu definition is unavailable.';
      screen.appendChild(empty);

      renderMenuDetail(null);
      syncMenuControls([]);
      return;
    }

    const currentMenu = getMenuNode(menuCurrentPath);
    const children = getVisibleMenuChildren(currentMenu);
    clampMenuSelection();

    path.textContent = buildMenuBreadcrumb(currentMenu);
    state.textContent = children.length + ' item' + (children.length === 1 ? '' : 's');
    state.className = 'menu-widget-state muted';

    screen.replaceChildren();

    if (children.length === 0) {
      screen.style.gridTemplateRows = '1fr';
      const empty = document.createElement('div');
      empty.className = 'menu-line-empty';
      empty.textContent = 'This menu is empty.';
      screen.appendChild(empty);
      renderMenuDetail(null);
      syncMenuControls(children);
      return;
    }

    screen.style.gridTemplateRows = 'repeat(' + children.length + ', minmax(0, 1fr))';

    children.forEach((child, actualIndex) => {
      const line = document.createElement('button');
      line.type = 'button';
      line.className = 'menu-line menu-line-' + child.kind + (actualIndex === menuSelectedIndex ? ' is-active' : '');
      line.setAttribute('role', 'option');
      line.setAttribute('aria-selected', actualIndex === menuSelectedIndex ? 'true' : 'false');
      line.title = child.raw_text;
      line.addEventListener('click', () => {
        menuSelectedIndex = actualIndex;
        renderMenuWidget();
        screen.focus();
      });
      line.addEventListener('dblclick', () => {
        menuSelectedIndex = actualIndex;
        if (isMenuNodeEditable(child)) {
          openMenuEditModal(child);
          return;
        }
        openSelectedMenuItem();
      });

      const label = document.createElement('span');
      label.className = 'menu-line-label';
      label.textContent = child.display_label || child.title || child.path;

      line.appendChild(label);

      const hintText = menuLineHint(child);
      if (hintText) {
        const hint = document.createElement('span');
        hint.className = 'menu-line-hint';
        hint.textContent = hintText;
        line.appendChild(hint);
      }

      screen.appendChild(line);
    });

    renderMenuDetail(getSelectedMenuNode());
    syncMenuControls(children);
  }

  function moveMenuSelection(delta) {
    const children = getCurrentMenuChildren();
    if (children.length === 0) {
      return;
    }

    menuSelectedIndex += delta;
    clampMenuSelection();
    renderMenuWidget();
    document.getElementById('menuScreen').focus();
  }

  function navigateToMenu(path, preferredChildPath = null) {
    const menuNode = getMenuNode(path);
    menuCurrentPath = menuNode.path;
    menuSelectedIndex = findSelectableChildIndex(menuNode, preferredChildPath);
    renderMenuWidget();
    document.getElementById('menuScreen').focus();
  }

  function openSiblingMenu(direction) {
    const currentMenu = getMenuNode(menuCurrentPath);
    if (!currentMenu || currentMenu.path === '') {
      return;
    }

    const parent = getMenuNode(currentMenu.parent_path || '');
    const siblingMenus = (parent.children || []).filter((child) => child.kind === 'menu');
    const currentIndex = siblingMenus.findIndex((child) => child.path === currentMenu.path);
    if (currentIndex === -1) {
      return;
    }

    const offset = direction === 'prev' ? -1 : 1;
    const target = siblingMenus[currentIndex + offset];
    if (!target) {
      return;
    }

    navigateToMenu(target.path);
  }

  function openSelectedMenuItem() {
    const selected = getSelectedMenuNode();
    if (!selected) {
      return;
    }

    if (selected.kind === 'menu') {
      navigateToMenu(selected.path);
      return;
    }

    if (selected.kind === 'page_link') {
      openSiblingMenu(selected.page_direction);
      return;
    }

    renderMenuWidget();
  }

  function goBackInMenu() {
    if (menuCurrentPath === '') {
      return;
    }

    const currentMenu = getMenuNode(menuCurrentPath);
    navigateToMenu(currentMenu.parent_path || '', currentMenu.path);
  }

  function goHomeInMenu() {
    navigateToMenu('');
  }

  function isChoiceButtonEditor(editor) {
    return editor.type === 'boolean' || (editor.type === 'enum' && editor.options.length <= 2);
  }

  function currentMenuEditNode() {
    return menuEditState.nodePath ? getMenuNode(menuEditState.nodePath) : null;
  }

  function closeMenuEditModal() {
    menuEditState = {
      open: false,
      nodePath: null,
      editor: null,
      draftValue: null
    };
    document.getElementById('menuEditModalBackdrop').classList.remove('open');
    document.getElementById('menuEditModalBackdrop').setAttribute('aria-hidden', 'true');
  }

  function renderMenuEditChoices() {
    const group = document.getElementById('menuEditChoiceGroup');
    group.replaceChildren();

    const editor = menuEditState.editor;
    if (!editor) {
      return;
    }

    editor.options.forEach((option) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className =
        'menu-edit-choice' + (String(option.value) === String(menuEditState.draftValue) ? ' is-selected' : '');
      button.textContent = option.label;
      button.addEventListener('click', () => {
        menuEditState.draftValue = option.value;
        renderMenuEditChoices();
      });
      group.appendChild(button);
    });
  }

  function openMenuEditModal(node) {
    const editor = getLeafEditor(node);
    if (!editor) {
      return;
    }

    menuEditState = {
      open: true,
      nodePath: node.path,
      editor,
      draftValue: getCurrentMenuValue(node, editor)
    };

    document.getElementById('menuEditModalTitle').textContent = 'Edit ' + (node.display_label || node.title);
    document.getElementById('menuEditModalPath').textContent = buildMenuBreadcrumb(node);
    document.getElementById('menuEditModalCurrent').textContent =
      'Current UI value: ' + formatMenuValue(node, menuEditState.draftValue, editor);
    document.getElementById('menuEditModalStatus').textContent =
      'UI-only edit. Save stores the value locally in this browser session.';
    document.getElementById('menuEditModalStatus').className = 'modal-status muted';

    const choiceGroup = document.getElementById('menuEditChoiceGroup');
    const numberField = document.getElementById('menuEditNumberField');
    const selectField = document.getElementById('menuEditSelectField');
    const numberInput = document.getElementById('menuEditNumberInput');
    const selectInput = document.getElementById('menuEditSelectInput');

    choiceGroup.hidden = true;
    numberField.hidden = true;
    selectField.hidden = true;
    choiceGroup.replaceChildren();
    selectInput.replaceChildren();

    if (isChoiceButtonEditor(editor)) {
      choiceGroup.hidden = false;
      renderMenuEditChoices();
    } else if (editor.type === 'enum') {
      selectField.hidden = false;
      editor.options.forEach((option) => {
        const element = document.createElement('option');
        element.value = String(option.value);
        element.textContent = option.label;
        if (String(option.value) === String(menuEditState.draftValue)) {
          element.selected = true;
        }
        selectInput.appendChild(element);
      });
    } else {
      numberField.hidden = false;
      numberInput.step = editor.step || (editor.type === 'float' ? 'any' : '1');
      numberInput.value = String(menuEditState.draftValue);
    }

    const backdrop = document.getElementById('menuEditModalBackdrop');
    backdrop.classList.add('open');
    backdrop.setAttribute('aria-hidden', 'false');

    if (choiceGroup.hidden === false) {
      const firstChoice = choiceGroup.querySelector('button');
      firstChoice?.focus();
    } else if (selectField.hidden === false) {
      selectInput.focus();
    } else {
      numberInput.focus();
      numberInput.select();
    }
  }

  function saveMenuEdit() {
    const node = currentMenuEditNode();
    const editor = menuEditState.editor;
    if (!node || !editor) {
      return;
    }

    const status = document.getElementById('menuEditModalStatus');
    let nextValue = menuEditState.draftValue;

    if (editor.type === 'enum' && !isChoiceButtonEditor(editor)) {
      const raw = document.getElementById('menuEditSelectInput').value;
      const selected = editor.options.find((option) => String(option.value) === raw);
      nextValue = selected ? selected.value : editor.options[0]?.value;
    } else if (editor.type === 'integer' || editor.type === 'float') {
      const raw = document.getElementById('menuEditNumberInput').value.trim();
      if (!raw) {
        status.textContent = 'Enter a value first.';
        status.className = 'modal-status err';
        return;
      }

      nextValue = editor.type === 'float' ? Number.parseFloat(raw) : Number.parseInt(raw, 10);
      if (!Number.isFinite(nextValue)) {
        status.textContent = 'Enter a valid number.';
        status.className = 'modal-status err';
        return;
      }
    } else if (nextValue === null || nextValue === undefined) {
      status.textContent = 'Choose a value first.';
      status.className = 'modal-status err';
      return;
    }

    menuValueStore.set(node.path, nextValue);
    closeMenuEditModal();
    renderMenuWidget();
    document.getElementById('menuScreen').focus();
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
  document.getElementById('menuEditModalBackdrop').addEventListener('click', (event) => {
    if (event.target.id === 'menuEditModalBackdrop') {
      closeMenuEditModal();
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
  document.getElementById('menuUpBtn').addEventListener('click', () => moveMenuSelection(-1));
  document.getElementById('menuDownBtn').addEventListener('click', () => moveMenuSelection(1));
  document.getElementById('menuEnterBtn').addEventListener('click', openSelectedMenuItem);
  document.getElementById('menuBackBtn').addEventListener('click', goBackInMenu);
  document.getElementById('menuHomeBtn').addEventListener('click', goHomeInMenu);
  document.getElementById('saveMenuEditBtn').addEventListener('click', saveMenuEdit);
  document.getElementById('cancelMenuEditBtn').addEventListener('click', closeMenuEditModal);
  document.getElementById('menuEditSelectInput').addEventListener('change', (event) => {
    const editor = menuEditState.editor;
    if (!editor) {
      return;
    }
    const selected = editor.options.find((option) => String(option.value) === event.target.value);
    if (selected) {
      menuEditState.draftValue = selected.value;
    }
  });
  document.getElementById('menuEditNumberInput').addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      saveMenuEdit();
    } else if (event.key === 'Escape') {
      event.preventDefault();
      closeMenuEditModal();
    }
  });
  document.getElementById('menuEditSelectInput').addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      saveMenuEdit();
    } else if (event.key === 'Escape') {
      event.preventDefault();
      closeMenuEditModal();
    }
  });
  document.getElementById('menuScreen').addEventListener('keydown', (event) => {
    if (event.key === 'ArrowUp') {
      event.preventDefault();
      moveMenuSelection(-1);
    } else if (event.key === 'ArrowDown') {
      event.preventDefault();
      moveMenuSelection(1);
    } else if (event.key === 'Enter' || event.key === 'ArrowRight') {
      event.preventDefault();
      openSelectedMenuItem();
    } else if (event.key === 'Escape' || event.key === 'ArrowLeft' || event.key === 'Backspace') {
      event.preventDefault();
      goBackInMenu();
    } else if (event.key === 'Home') {
      event.preventDefault();
      goHomeInMenu();
    }
  });

  initializeMenuWidget();
  refresh();
  setInterval(refresh, 1000);
