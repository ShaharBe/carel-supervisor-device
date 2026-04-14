window.CarelMenuWidget = (() => {
  let refreshPage = null;
  let listenersBound = false;
  let baseMenuPayload = null;
  let baseMenuIndex = new Map();
  let menuPayload = null;
  let menuIndex = new Map();
  let menuCurrentPath = '';
  let menuSelectedIndex = 0;
  const menuValueStore = new Map();
  const menuValueStateStore = new Map();
  let menuValueRequestSequence = 0;
  const measureUnitsPath = '3.2.1.5';
  let menuRuleDriverPaths = [];
  let dashboardSyncMap = {};
  let lastMenuRuleDriverRefreshAt = 0;
  const menuRuleDriverRefreshIntervalMs = 5000;
    const unitProfiles = {
      metric: {
        temperature: '\u00B0C',
        production_rate: 'kg/h',
        amps: 'A',
        percent: '%',
        hours: 'h',
        seconds: 'sec',
        days: 'd',
        conductivity: 'uS/cm'
      },
      imperial: {
        temperature: '\u00B0F',
        production_rate: 'lb/h',
        amps: 'A',
        percent: '%',
        hours: 'h',
        seconds: 'sec',
        days: 'd',
        conductivity: 'uS/cm'
      }
  };
  let menuEditState = {
    open: false,
    nodePath: null,
    editor: null,
    draftValue: null,
    busy: false
  };
  const menuDisplayStorageKey = 'carel-menu-display-settings';
  const menuFontFamilies = {
    current: 'Consolas, "Courier New", monospace',
    bubbledot: '"Bubbledot", Consolas, "Courier New", monospace'
  };
  const defaultMenuDisplaySettings = {
    sizePercent: 100,
    widthPercent: 88,
    fontFamily: 'current'
  };
  let menuDisplaySettings = { ...defaultMenuDisplaySettings };
  const menuLocationStorageKey = 'carel-menu-location';
  const defaultMenuLongPressDurationMs = 1000;
  let menuLongPressDurationMs = defaultMenuLongPressDurationMs;

  function clampNumber(value, min, max, fallback) {
    const numericValue = Number(value);
    if (!Number.isFinite(numericValue)) {
      return fallback;
    }
    return Math.min(max, Math.max(min, numericValue));
  }

  function sanitizeMenuDisplaySettings(rawSettings) {
    const fontFamily = String(rawSettings?.fontFamily || defaultMenuDisplaySettings.fontFamily);
    return {
      sizePercent: clampNumber(rawSettings?.sizePercent, 80, 180, defaultMenuDisplaySettings.sizePercent),
      widthPercent: clampNumber(rawSettings?.widthPercent, 70, 110, defaultMenuDisplaySettings.widthPercent),
      fontFamily: Object.prototype.hasOwnProperty.call(menuFontFamilies, fontFamily)
        ? fontFamily
        : defaultMenuDisplaySettings.fontFamily
    };
  }

  function loadMenuDisplaySettings() {
    try {
      const saved = window.localStorage.getItem(menuDisplayStorageKey);
      if (!saved) {
        return { ...defaultMenuDisplaySettings };
      }
      return sanitizeMenuDisplaySettings(JSON.parse(saved));
    } catch (e) {
      return { ...defaultMenuDisplaySettings };
    }
  }

  function persistMenuDisplaySettings() {
    try {
      window.localStorage.setItem(menuDisplayStorageKey, JSON.stringify(menuDisplaySettings));
    } catch (e) {
      // Ignore storage failures; the controls can still work for the current page session.
    }
  }

  function getMenuBaseFontRem() {
    return window.matchMedia('(max-width: 600px)').matches ? 1.3 : 1.45;
  }

  function menuLetterSpacingEm(widthPercent) {
    return (widthPercent - 100) / 800;
  }

  function syncMenuDisplayControls() {
    document.getElementById('menuFontSizeRange').value = String(menuDisplaySettings.sizePercent);
    document.getElementById('menuFontWidthRange').value = String(menuDisplaySettings.widthPercent);
    document.getElementById('menuFontFamilySelect').value = menuDisplaySettings.fontFamily;
    document.getElementById('menuFontSizeValue').textContent = menuDisplaySettings.sizePercent + '%';
    document.getElementById('menuFontWidthValue').textContent = menuDisplaySettings.widthPercent + '%';
  }

  function applyMenuDisplaySettings() {
    const screen = document.getElementById('menuScreen');
    const fontSizeRem = (getMenuBaseFontRem() * menuDisplaySettings.sizePercent / 100).toFixed(3);
    const letterSpacing = menuLetterSpacingEm(menuDisplaySettings.widthPercent).toFixed(3);
    const textScale = (menuDisplaySettings.widthPercent / 100).toFixed(3);

    screen.style.setProperty('--menu-font-size', fontSizeRem + 'rem');
    screen.style.setProperty('--menu-font-family', menuFontFamilies[menuDisplaySettings.fontFamily]);
    screen.style.setProperty('--menu-text-scale-x', textScale);
    screen.style.setProperty('--menu-letter-spacing', letterSpacing + 'em');
    syncMenuDisplayControls();
  }

  function initializeMenuDisplaySettings() {
    menuDisplaySettings = loadMenuDisplaySettings();
    applyMenuDisplaySettings();
  }

  function updateMenuDisplaySetting(patch) {
    menuDisplaySettings = sanitizeMenuDisplaySettings({ ...menuDisplaySettings, ...patch });
    applyMenuDisplaySettings();
    persistMenuDisplaySettings();
  }

  function resetMenuDisplaySettings() {
    menuDisplaySettings = { ...defaultMenuDisplaySettings };
    applyMenuDisplaySettings();
    persistMenuDisplaySettings();
  }

  function loadMenuLocation() {
    try {
      const saved = window.sessionStorage?.getItem(menuLocationStorageKey);
      if (!saved) {
        return null;
      }

      const location = JSON.parse(saved);
      const selectedIndex = Number(location?.selectedIndex);
      return {
        path: typeof location?.path === 'string' ? location.path : '',
        selectedIndex: Number.isInteger(selectedIndex) && selectedIndex >= 0 ? selectedIndex : 0
      };
    } catch (e) {
      return null;
    }
  }

  function persistMenuLocation() {
    try {
      window.sessionStorage?.setItem(
        menuLocationStorageKey,
        JSON.stringify({ path: menuCurrentPath, selectedIndex: menuSelectedIndex })
      );
    } catch (e) {
      // Ignore storage failures; menu navigation still works for the current page session.
    }
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

  function indexMenuTree(node, parentPath, targetIndex = menuIndex) {
    if (!node) {
      return;
    }

    node.parent_path = parentPath;
    targetIndex.set(node.path, node);
    (node.children || []).forEach((child) => indexMenuTree(child, node.path, targetIndex));
  }

  function initializeMenuWidget() {
    baseMenuPayload = parseMenuPayload();
    baseMenuIndex = new Map();
    indexMenuTree(baseMenuPayload.root, null, baseMenuIndex);
    dashboardSyncMap = baseMenuPayload.dashboard_sync_map || {};
    menuRuleDriverPaths = collectMenuRuleDriverPaths();
    rebuildRuntimeMenuTree();
    restoreMenuLocation();
    renderMenuWidget();
    refreshVisibleMenuLeafValues(true);
  }

  function walkMenuNodes(node, visitor) {
    if (!node) {
      return;
    }

    visitor(node);
    (node.children || []).forEach((child) => walkMenuNodes(child, visitor));
  }

  function collectMenuRuleDriverPaths() {
    if (!baseMenuPayload?.ok) {
      return [];
    }

    const paths = new Set();
    walkMenuNodes(baseMenuPayload.root, (node) => {
      if (node.visible_if?.path) {
        paths.add(String(node.visible_if.path));
      }
      if (node.quantity) {
        paths.add(node.unit_source_path || measureUnitsPath);
      }
    });
    return Array.from(paths);
  }

  function getBaseMenuNode(path) {
    return baseMenuIndex.get(path) || baseMenuPayload?.root || null;
  }

  function cloneMenuRoot(root) {
    return JSON.parse(JSON.stringify(root));
  }

  function resolveMeasureUnitProfile(rawValue) {
    const isImperial =
      rawValue === true ||
      rawValue === 1 ||
      rawValue === '1' ||
      String(rawValue).toLowerCase() === 'true';
    return isImperial ? unitProfiles.imperial : unitProfiles.metric;
  }

  function evaluateVisibleRule(rule) {
    if (!rule || !rule.path) {
      return true;
    }

    const currentValue = menuValueStore.get(String(rule.path));
    if (currentValue === undefined) {
      return false;
    }

    const operator = rule.operator || 'equals';
    const values = Array.isArray(rule.values) ? rule.values : [];
    if (operator === 'in') {
      return values.some((candidate) => String(candidate) === String(currentValue));
    }
    if (operator === 'equals') {
      const expected = values.length > 0 ? values[0] : rule.value;
      return String(expected) === String(currentValue);
    }
    return true;
  }

  function decorateRangeHint(node, rangeHint) {
    if (!rangeHint) {
      return rangeHint;
    }

    if (node.display_unit && isNumericRangeHint(rangeHint)) {
      return formatValueWithUnit(rangeHint, node.display_unit);
    }

    return rangeHint;
  }

  function formatValueWithUnit(valueText, unit) {
    if (!unit) {
      return valueText;
    }

    const compactUnits = new Set(['%', 'h', 'd']);
    return compactUnits.has(unit) ? valueText + unit : valueText + ' ' + unit;
  }

  function applyRuntimeNodeDecorations(node, runtimeContext) {
    const baseVisible = node.visible !== false;
    node.visible = baseVisible && evaluateVisibleRule(node.visible_if);
    node.display_unit = null;
    node.display_range_or_options = node.range_or_options;

    if (node.quantity) {
      const unitProfile = runtimeContext.unitProfiles[node.unit_source_path || measureUnitsPath] || runtimeContext.defaultUnitProfile;
      node.display_unit = unitProfile?.[node.quantity] || null;
      node.display_range_or_options = decorateRangeHint(node, node.range_or_options);
    }

    (node.children || []).forEach((child) => applyRuntimeNodeDecorations(child, runtimeContext));
  }

  function isMenuNodeAndAncestorsVisible(path) {
    let current = getMenuNode(path);
    if (!current) {
      return false;
    }

    while (current) {
      if (current.path !== '' && !isMenuNodeVisible(current)) {
        return false;
      }
      if (current.parent_path === null) {
        break;
      }
      current = getMenuNode(current.parent_path);
    }

    return true;
  }

  function reconcileMenuLocation() {
    if (isMenuNodeAndAncestorsVisible(menuCurrentPath)) {
      return;
    }

    let currentPath = menuCurrentPath;
    while (currentPath) {
      const currentNode = getMenuNode(currentPath);
      const parentPath = currentNode?.parent_path || '';
      if (isMenuNodeAndAncestorsVisible(parentPath)) {
        menuCurrentPath = parentPath;
        menuSelectedIndex = findSelectableChildIndex(getMenuNode(parentPath), currentPath);
        persistMenuLocation();
        return;
      }
      currentPath = parentPath;
    }

    menuCurrentPath = '';
    menuSelectedIndex = 0;
    persistMenuLocation();
  }

  function rebuildRuntimeMenuTree() {
    if (!baseMenuPayload) {
      return;
    }

    if (!baseMenuPayload.ok) {
      menuPayload = baseMenuPayload;
      menuIndex = new Map();
      indexMenuTree(menuPayload.root, null, menuIndex);
      return;
    }

    const runtimeRoot = cloneMenuRoot(baseMenuPayload.root);
    const runtimeContext = {
      defaultUnitProfile: resolveMeasureUnitProfile(menuValueStore.get(measureUnitsPath)),
      unitProfiles: {
        [measureUnitsPath]: resolveMeasureUnitProfile(menuValueStore.get(measureUnitsPath))
      }
    };

    applyRuntimeNodeDecorations(runtimeRoot, runtimeContext);
    menuPayload = {
      ...baseMenuPayload,
      root: runtimeRoot
    };
    menuIndex = new Map();
    indexMenuTree(menuPayload.root, null, menuIndex);
    reconcileMenuLocation();
  }

  function getMenuNode(path) {
    return menuIndex.get(path) || menuPayload.root;
  }

  function restoreMenuLocation() {
    const savedLocation = loadMenuLocation();
    const savedPath = savedLocation?.path || '';
    const savedNode = savedPath === '' ? menuPayload?.root : menuIndex.get(savedPath);

    if (!savedLocation || !savedNode || (savedPath !== '' && savedNode.kind !== 'menu')) {
      menuCurrentPath = '';
      menuSelectedIndex = 0;
      return;
    }

    menuCurrentPath = savedPath;
    menuSelectedIndex = savedLocation.selectedIndex;
    if (!isMenuNodeAndAncestorsVisible(menuCurrentPath)) {
      reconcileMenuLocation();
      return;
    }

    clampMenuSelection();
  }

  function isMenuNodeVisible(node) {
    return node?.visible !== false;
  }

  function isPageGroupMenu(node) {
    return node?.kind === 'menu' && Boolean(node?.page_group);
  }

  function stripPageCountSuffix(label) {
    return String(label || '').replace(/\s*\(\d+\s*\/\s*\d+\)\s*$/, '').trim();
  }

  function getPageGroupBaseLabel(node) {
    return node?.page_group_label || stripPageCountSuffix(node?.display_label || node?.title || node?.path || '');
  }

  function getMenuNodeDisplayLabel(node) {
    if (!node) {
      return '';
    }

    if (isPageGroupMenu(node)) {
      return getPageGroupBaseLabel(node) + ' (' + node.page_index + '/' + node.page_count + ')';
    }

    return node.display_label || node.title || node.path;
  }

  function getCurrentMenuTitleText(node) {
    if (!node || node.path === '') {
      return '-- Main Menu --';
    }

    return getMenuNodeDisplayLabel(node);
  }

  function getParentListMenuLabel(node) {
    if (!node) {
      return '';
    }

    if (isPageGroupMenu(node)) {
      return getPageGroupBaseLabel(node);
    }

    return getMenuNodeDisplayLabel(node);
  }

  function getPageGroupMenuSiblings(node) {
    if (!isPageGroupMenu(node)) {
      return [];
    }

    const parent = getMenuNode(node.parent_path || '');
    return (parent.children || [])
      .filter(
        (child) => child.kind === 'menu' && isMenuNodeVisible(child) && child.page_group === node.page_group
      )
      .sort((left, right) => Number(left.page_index || 0) - Number(right.page_index || 0));
  }

  function isMenuNodeListedInParent(node) {
    if (!isMenuNodeVisible(node)) {
      return false;
    }

    if (isPageGroupMenu(node)) {
      return Number(node.page_index || 1) === 1;
    }

    return true;
  }

  function getListedMenuChildren(node) {
    return (node.children || []).filter((child) => isMenuNodeListedInParent(child));
  }

  function getCurrentMenuChildren() {
    return getListedMenuChildren(getMenuNode(menuCurrentPath));
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
    if (node.kind === 'leaf') {
      return formatMenuLineValue(node);
    }
    return '';
  }

  function buildMenuBreadcrumb(node) {
    const labels = [];
    let current = node;
    while (current && current.path !== '') {
      labels.unshift(getMenuNodeDisplayLabel(current));
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
    const label = getMenuNodeDisplayLabel(node);
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

    // Prefer the backend-resolved editor when present.
    const resolved = node.resolved_editor;
    if (resolved && resolved.type) {
      return {
        type: resolved.type,
        options: resolved.options || [],
        currentValue: node.editor?.current_value,
        step: resolved.step,
        scale: resolved.scale,
        limits: resolved.limits
      };
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

  function isMenuNodeModbusBacked(node) {
    if (node?.resolved_editor) {
      return node.resolved_editor.modbus_backed;
    }
    return ['A', 'I', 'D'].includes(node?.register?.family);
  }

  function isMenuNodeWritable(node) {
    if (node?.resolved_editor) {
      return node.resolved_editor.writable;
    }
    return node?.register?.access === 'R/W';
  }

  function menuNodeSupportsRemoteRead(node) {
    return isMenuNodeModbusBacked(node);
  }

  function menuNodeSupportsRemoteWrite(node) {
    return isMenuNodeModbusBacked(node) && isMenuNodeWritable(node);
  }

  function isMenuNodeEditable(node) {
    if (node?.resolved_editor) {
      return node.resolved_editor.editable;
    }

    if (['menu', 'caption', 'page_link'].includes(node?.kind)) {
      return false;
    }

    if (getLeafEditor(node) === null) {
      return false;
    }

    if (isMenuNodeModbusBacked(node)) {
      return isMenuNodeWritable(node);
    }

    return true;
  }

  function getDefaultNumericValue(node, editor) {
    const match = (node.range_or_options || '').match(/-?\d+(?:\.\d+)?/);
    if (match) {
      return editor.type === 'float' ? Number.parseFloat(match[0]) : Number.parseInt(match[0], 10);
    }

    return 0;
  }

  function getMenuValueState(path) {
    return menuValueStateStore.get(path) || { loading: false, error: null };
  }

  function setMenuValueState(path, patch) {
    const current = getMenuValueState(path);
    menuValueStateStore.set(path, { ...current, ...patch });
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

  function resolveDotPath(obj, dotPath) {
    let current = obj;
    for (const key of dotPath.split('.')) {
      if (current == null || typeof current !== 'object') {
        return undefined;
      }
      current = current[key];
    }
    return current;
  }

  function syncMenuCacheFromDashboard(payload) {
    for (const [menuPath, payloadKey] of Object.entries(dashboardSyncMap)) {
      const value = resolveDotPath(payload, payloadKey);
      if (value !== null && value !== undefined) {
        menuValueStore.set(menuPath, value);
      }
    }
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

    if (editor.type === 'integer') {
      const numericValue = Number(value);
      if (Number.isFinite(numericValue)) {
        const formatted = String(Math.round(numericValue));
        return formatValueWithUnit(formatted, node.display_unit);
      }
    }

    if (editor.type === 'float') {
      const numericValue = Number(value);
      if (Number.isFinite(numericValue)) {
        const formatted = numericValue.toFixed(1);
        return formatValueWithUnit(formatted, node.display_unit);
      }
    }

    return String(value);
  }

  function formatMenuLineValue(node) {
    const editor = getLeafEditor(node);
    const valueState = getMenuValueState(node.path);
    if (menuValueStore.has(node.path)) {
      if (!editor) {
        return String(menuValueStore.get(node.path));
      }
      return formatMenuValue(node, menuValueStore.get(node.path), editor);
    }

    if (!editor) {
      return '--';
    }

    if (valueState.loading) {
      return '...';
    }

    if (valueState.error) {
      return 'ERR';
    }

    if (menuNodeSupportsRemoteRead(node)) {
      return '...';
    }

    return '--';
  }

  function menuRowTrack(node) {
    if (node?.kind === 'caption') {
      return 'max-content';
    }

    return 'minmax(0, 1fr)';
  }

  async function refreshVisibleMenuLeafValues(forceRefresh = true) {
    const activeMenuPath = menuCurrentPath;
    const requestSequence = ++menuValueRequestSequence;
    const visibleLeaves = getCurrentMenuChildren().filter(
      (child) => child.kind === 'leaf' && menuNodeSupportsRemoteRead(child)
    );
    const targetLeaves = forceRefresh
      ? visibleLeaves
      : visibleLeaves.filter((child) => !menuValueStore.has(child.path));

    if (targetLeaves.length === 0) {
      return;
    }

    targetLeaves.forEach((node) => {
      setMenuValueState(node.path, { loading: true, error: null });
    });
    renderMenuWidget();

    for (const node of targetLeaves) {
      if (menuCurrentPath !== activeMenuPath || requestSequence !== menuValueRequestSequence) {
        break;
      }

      try {
        await fetchMenuNodeValue(node, { refresh: true });
        setMenuValueState(node.path, { loading: false, error: null });
      } catch (error) {
        setMenuValueState(node.path, { loading: false, error: error.message });
      }

      if (menuCurrentPath === activeMenuPath && requestSequence === menuValueRequestSequence) {
        renderMenuWidget();
      }
    }
  }

  function menuDetailMeta(node) {
    const fragments = [];
    if (node.kind === 'menu') {
      const count = getListedMenuChildren(node).length;
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

    if (node.display_unit) {
      fragments.push('Unit: ' + node.display_unit);
    }

    if (node.display_range_or_options || node.range_or_options) {
      fragments.push('Options/range: ' + (node.display_range_or_options || node.range_or_options));
    }

    return fragments.join(' | ');
  }

  function formatMenuNote(note) {
    if (Array.isArray(note)) {
      return note.map((line) => (line === null || line === undefined ? '' : String(line))).join('\n').trim();
    }

    if (note === null || note === undefined) {
      return '';
    }

    return String(note).trim();
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
    title.textContent = getMenuNodeDisplayLabel(node) || 'Unnamed item';
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
    } else if (menuNodeSupportsRemoteWrite(node)) {
      guidance.textContent = 'Double-click to load the current controller value and edit it.';
    } else if (isMenuNodeEditable(node)) {
      guidance.textContent = 'Double-click to edit this value locally.';
    } else if (menuNodeSupportsRemoteRead(node)) {
      guidance.textContent = 'This leaf is mapped to Modbus, but it is read-only.';
    } else {
      guidance.textContent = 'This leaf is not mapped to Modbus yet.';
    }
    detail.appendChild(guidance);

    if (node.kind === 'leaf') {
      const currentValue = document.createElement('div');
      currentValue.className = 'menu-detail-note';
      currentValue.textContent = 'Current value: ' + formatMenuLineValue(node);
      detail.appendChild(currentValue);

      const valueState = getMenuValueState(node.path);
      if (valueState.error) {
        const errorText = document.createElement('div');
        errorText.className = 'menu-detail-note err';
        errorText.textContent = 'Read error: ' + valueState.error;
        detail.appendChild(errorText);
      }
    }

    const noteText = formatMenuNote(node.note);
    if (noteText) {
      const note = document.createElement('div');
      note.className = 'menu-detail-note';
      note.textContent = 'Note: ' + noteText;
      detail.appendChild(note);
    }

    const raw = document.createElement('div');
    raw.className = 'menu-detail-raw';
    raw.textContent = 'Definition: ' + node.raw_text;
    detail.appendChild(raw);
  }

  function syncMenuControls(children) {
    const hasSelection = children.length > 0;
    document.getElementById('menuBackBtn').disabled = menuCurrentPath === '';
    document.getElementById('menuHomeBtn').disabled = menuCurrentPath === '';
    const prevButton = document.getElementById('menuPagePrevBtn');
    const nextButton = document.getElementById('menuPageNextBtn');
    const currentMenu = getMenuNode(menuCurrentPath);
    const pageTargets = getCurrentPageTargets();
    const inPageGroup = isPageGroupMenu(currentMenu);

    prevButton.hidden = !inPageGroup;
    prevButton.disabled = pageTargets.prev === null;
    nextButton.hidden = !inPageGroup;
    nextButton.disabled = pageTargets.next === null;
  }

  function findSelectableChildIndex(menuNode, preferredChildPath) {
    const listedChildren = getListedMenuChildren(menuNode);
    if (listedChildren.length === 0) {
      return 0;
    }

    if (!preferredChildPath) {
      return 0;
    }

    const directIndex = listedChildren.findIndex((child) => child.path === preferredChildPath);
    if (directIndex !== -1) {
      return directIndex;
    }

    const allChildren = menuNode.children || [];
    const targetIndex = allChildren.findIndex((child) => child.path === preferredChildPath);
    if (targetIndex === -1) {
      return 0;
    }

    for (let index = targetIndex - 1; index >= 0; index -= 1) {
      if (isMenuNodeListedInParent(allChildren[index])) {
        return listedChildren.findIndex((child) => child.path === allChildren[index].path);
      }
    }

    for (let index = targetIndex + 1; index < allChildren.length; index += 1) {
      if (isMenuNodeListedInParent(allChildren[index])) {
        return listedChildren.findIndex((child) => child.path === allChildren[index].path);
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
    const children = getListedMenuChildren(currentMenu);
    const titleRow = {
      kind: 'caption',
      title: getCurrentMenuTitleText(currentMenu),
      display_label: getCurrentMenuTitleText(currentMenu),
      raw_text: '(Generated) Current page title'
    };
    const displayRows = [titleRow, ...children];
    clampMenuSelection();

    path.textContent = buildMenuBreadcrumb(currentMenu);
    state.textContent = children.length + ' item' + (children.length === 1 ? '' : 's');
    state.className = 'menu-widget-state muted';

    screen.replaceChildren();

    if (children.length === 0) {
      screen.style.gridTemplateRows = menuRowTrack(titleRow) + ' 1fr';
      const header = document.createElement('div');
      header.className = 'menu-line menu-line-caption menu-line-static';
      header.setAttribute('role', 'presentation');

      const headerLabel = document.createElement('span');
      headerLabel.className = 'menu-line-label';
      headerLabel.textContent = titleRow.display_label;
      header.appendChild(headerLabel);
      screen.appendChild(header);

      const empty = document.createElement('div');
      empty.className = 'menu-line-empty';
      empty.textContent = 'This menu is empty.';
      screen.appendChild(empty);
      renderMenuDetail(null);
      syncMenuControls(children);
      return;
    }

    screen.style.gridTemplateRows = displayRows.map((child) => menuRowTrack(child)).join(' ');

    const header = document.createElement('div');
    header.className = 'menu-line menu-line-caption menu-line-static';
    header.setAttribute('role', 'presentation');

    const headerLabel = document.createElement('span');
    headerLabel.className = 'menu-line-label';
    headerLabel.textContent = titleRow.display_label;
    header.appendChild(headerLabel);
    screen.appendChild(header);

    children.forEach((child, actualIndex) => {
      const line = document.createElement('button');
      line.type = 'button';
      line.className = 'menu-line menu-line-' + child.kind + (actualIndex === menuSelectedIndex ? ' is-active' : '');
      line.setAttribute('role', 'option');
      line.setAttribute('aria-selected', actualIndex === menuSelectedIndex ? 'true' : 'false');
      line.title = child.raw_text;
      const longPressState = bindMenuLongPress(line, child, actualIndex);
      line.addEventListener('click', (event) => {
        if (longPressState.consumeClick(event)) {
          return;
        }
        menuSelectedIndex = actualIndex;
        persistMenuLocation();
        renderMenuWidget();
        screen.focus();
      });
      line.addEventListener('dblclick', () => {
        activateMenuRow(child, actualIndex);
      });

      const label = document.createElement('span');
      label.className = 'menu-line-label';
      label.textContent = child.kind === 'menu' ? getParentListMenuLabel(child) : getMenuNodeDisplayLabel(child);

      line.appendChild(label);

      const hintText = menuLineHint(child);
      if (hintText) {
        const hint = document.createElement('span');
        hint.className = child.kind === 'leaf' ? 'menu-line-value' : 'menu-line-hint';
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
    persistMenuLocation();
    renderMenuWidget();
    document.getElementById('menuScreen').focus();
  }

  function navigateToMenu(path, preferredChildPath = null) {
    const menuNode = getMenuNode(path);
    menuCurrentPath = menuNode.path;
    menuSelectedIndex = findSelectableChildIndex(menuNode, preferredChildPath);
    persistMenuLocation();
    renderMenuWidget();
    refreshVisibleMenuLeafValues(true);
    document.getElementById('menuScreen').focus();
  }

  function openSiblingMenu(direction) {
    const currentMenu = getMenuNode(menuCurrentPath);
    if (!currentMenu || currentMenu.path === '') {
      return;
    }

    const siblingMenus = isPageGroupMenu(currentMenu)
      ? getPageGroupMenuSiblings(currentMenu)
      : (getMenuNode(currentMenu.parent_path || '').children || []).filter(
          (child) => child.kind === 'menu' && isMenuNodeVisible(child)
        );
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

    renderMenuWidget();
  }

  function activateMenuRow(child, actualIndex) {
    menuSelectedIndex = actualIndex;
    persistMenuLocation();
    if (isMenuNodeEditable(child)) {
      openMenuEditModal(child);
      return;
    }
    openSelectedMenuItem();
  }

  function bindMenuLongPress(line, child, actualIndex) {
    let timerId = null;
    let pointerId = null;
    let startX = 0;
    let startY = 0;
    let suppressClick = false;
    let suppressClickTimer = null;
    const maxMovePx = 12;

    function clearLongPressTimer() {
      if (timerId !== null) {
        window.clearTimeout(timerId);
        timerId = null;
      }
      pointerId = null;
    }

    function clearSuppressClick() {
      suppressClick = false;
      if (suppressClickTimer !== null) {
        window.clearTimeout(suppressClickTimer);
        suppressClickTimer = null;
      }
    }

    function armSuppressClick() {
      clearSuppressClick();
      suppressClick = true;
      suppressClickTimer = window.setTimeout(clearSuppressClick, 700);
    }

    function isLongPressPointer(event) {
      return event.pointerType === 'touch' || event.pointerType === 'pen';
    }

    line.addEventListener('pointerdown', (event) => {
      if (!isLongPressPointer(event)) {
        return;
      }
      clearLongPressTimer();
      pointerId = event.pointerId;
      startX = Number(event.clientX) || 0;
      startY = Number(event.clientY) || 0;
      timerId = window.setTimeout(() => {
        timerId = null;
        armSuppressClick();
        activateMenuRow(child, actualIndex);
      }, menuLongPressDurationMs);
    });

    line.addEventListener('pointermove', (event) => {
      if (timerId === null || event.pointerId !== pointerId) {
        return;
      }

      const deltaX = (Number(event.clientX) || 0) - startX;
      const deltaY = (Number(event.clientY) || 0) - startY;
      if (Math.hypot(deltaX, deltaY) > maxMovePx) {
        clearLongPressTimer();
      }
    });

    ['pointerup', 'pointercancel', 'pointerleave'].forEach((eventName) => {
      line.addEventListener(eventName, (event) => {
        if (event.pointerId === pointerId) {
          clearLongPressTimer();
        }
      });
    });

    line.addEventListener('contextmenu', (event) => {
      if (suppressClick || timerId !== null) {
        event.preventDefault();
      }
    });

    return {
      consumeClick(event) {
        if (!suppressClick) {
          return false;
        }
        event.preventDefault();
        event.stopPropagation();
        clearSuppressClick();
        return true;
      }
    };
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

  function getCurrentPageTargets() {
    const currentMenu = getMenuNode(menuCurrentPath);
    if (!isPageGroupMenu(currentMenu)) {
      return { prev: null, next: null };
    }

    const siblings = getPageGroupMenuSiblings(currentMenu);
    const currentIndex = siblings.findIndex((child) => child.path === currentMenu.path);
    if (currentIndex === -1) {
      return { prev: null, next: null };
    }

    return {
      prev: siblings[currentIndex - 1] || null,
      next: siblings[currentIndex + 1] || null
    };
  }

  function isChoiceButtonEditor(editor) {
    return editor.type === 'boolean' || (editor.type === 'enum' && editor.options.length <= 2);
  }

  function currentMenuEditNode() {
    return menuEditState.nodePath ? getMenuNode(menuEditState.nodePath) : null;
  }

  function setMenuEditStatus(message, tone = 'muted') {
    const status = document.getElementById('menuEditModalStatus');
    status.textContent = message;
    status.className = 'modal-status ' + tone;
  }

  function updateMenuEditCurrentText() {
    const node = currentMenuEditNode();
    const editor = menuEditState.editor;
    if (!node || !editor) {
      return;
    }

    document.getElementById('menuEditModalCurrent').textContent =
      'Current value: ' + formatMenuValue(node, menuEditState.draftValue, editor);
  }

  function syncMenuEditBusyState() {
    const busy = menuEditState.busy;
    document.getElementById('saveMenuEditBtn').disabled = busy;
    document.getElementById('menuEditNumberInput').disabled = busy;
    document.getElementById('menuEditSelectInput').disabled = busy;
    document.querySelectorAll('#menuEditChoiceGroup .menu-edit-choice').forEach((button) => {
      button.disabled = busy;
    });
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
      button.disabled = menuEditState.busy;
      button.addEventListener('click', () => {
        menuEditState.draftValue = option.value;
        renderMenuEditChoices();
      });
      group.appendChild(button);
    });
  }

  function populateMenuEditForm() {
    const editor = menuEditState.editor;
    const node = currentMenuEditNode();
    if (!editor) {
      return;
    }

    const choiceGroup = document.getElementById('menuEditChoiceGroup');
    const numberField = document.getElementById('menuEditNumberField');
    const selectField = document.getElementById('menuEditSelectField');
    const numberInput = document.getElementById('menuEditNumberInput');
    const selectInput = document.getElementById('menuEditSelectInput');
    const numberLabel = document.querySelector('label[for="menuEditNumberInput"]');
    const selectLabel = document.querySelector('label[for="menuEditSelectInput"]');

    if (numberLabel) {
      numberLabel.textContent = node?.display_unit ? 'Value (' + node.display_unit + ')' : 'Value';
    }
    if (selectLabel) {
      selectLabel.textContent = 'Value';
    }

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
      numberInput.value =
        menuEditState.draftValue === null || menuEditState.draftValue === undefined
          ? ''
          : String(menuEditState.draftValue);
    }

    syncMenuEditBusyState();
  }

  function focusMenuEditField() {
    const choiceGroup = document.getElementById('menuEditChoiceGroup');
    const numberField = document.getElementById('menuEditNumberField');
    const selectField = document.getElementById('menuEditSelectField');
    const numberInput = document.getElementById('menuEditNumberInput');
    const selectInput = document.getElementById('menuEditSelectInput');

    if (choiceGroup.hidden === false) {
      const firstChoice = choiceGroup.querySelector('button');
      firstChoice?.focus();
    } else if (selectField.hidden === false) {
      selectInput.focus();
    } else if (numberField.hidden === false) {
      numberInput.focus();
      numberInput.select();
    }
  }

  async function fetchMenuNodeValue(node, { refresh = true } = {}) {
    const query = new URLSearchParams({ path: node.path });
    if (refresh) {
      query.set('refresh', '1');
    }

    const response = await fetch('api/menu-value?' + query.toString());
    const payload = await response.json();
    if (!payload.ok) {
      throw new Error(payload.error || 'Unable to read menu value.');
    }

    // Merge resolved editor metadata from the API onto the base node so
    // subsequent UI renders use the authoritative backend metadata.
    if (payload.resolved_editor) {
      const baseNode = baseMenuIndex.get(node.path);
      if (baseNode) {
        baseNode.resolved_editor = payload.resolved_editor;
      }
      node.resolved_editor = payload.resolved_editor;
    }

    menuValueStore.set(node.path, payload.value);
    return payload;
  }

  async function saveMenuNodeValue(node, value) {
    const response = await fetch('api/menu-value', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: node.path, value })
    });
    const payload = await response.json();
    if (!payload.ok) {
      throw new Error(payload.error || 'Unable to write menu value.');
    }

    menuValueStore.set(node.path, payload.value);
    return payload;
  }

  function isMenuRuleDriverPath(path) {
    return menuRuleDriverPaths.includes(path);
  }

  async function refreshMenuRuleDrivers(forceRefresh = false) {
    if (!baseMenuPayload?.ok || menuRuleDriverPaths.length === 0) {
      return;
    }

    const now = Date.now();
    if (!forceRefresh && now - lastMenuRuleDriverRefreshAt < menuRuleDriverRefreshIntervalMs) {
      return;
    }

    lastMenuRuleDriverRefreshAt = now;
    for (const path of menuRuleDriverPaths) {
      const node = getBaseMenuNode(path);
      if (!node || !menuNodeSupportsRemoteRead(node)) {
        continue;
      }

      setMenuValueState(path, { loading: true, error: null });
      try {
        await fetchMenuNodeValue(node, { refresh: true });
        setMenuValueState(path, { loading: false, error: null });
      } catch (error) {
        setMenuValueState(path, { loading: false, error: error.message });
      }
    }
  }

  function closeMenuEditModal() {
    menuEditState = {
      open: false,
      nodePath: null,
      editor: null,
      draftValue: null,
      busy: false
    };
    document.getElementById('menuEditModalBackdrop').classList.remove('open');
    document.getElementById('menuEditModalBackdrop').setAttribute('aria-hidden', 'true');
  }

  async function openMenuEditModal(node) {
    const editor = getLeafEditor(node);
    if (!editor) {
      return;
    }

    // Cancel any remaining page-value refresh queue so the modal read can take priority.
    menuValueRequestSequence += 1;

    menuEditState = {
      open: true,
      nodePath: node.path,
      editor,
      draftValue: getCurrentMenuValue(node, editor),
      busy: false
    };

    document.getElementById('menuEditModalTitle').textContent = 'Edit ' + getMenuNodeDisplayLabel(node);
    document.getElementById('menuEditModalPath').textContent = buildMenuBreadcrumb(node);
    updateMenuEditCurrentText();
    if (menuNodeSupportsRemoteWrite(node)) {
      setMenuEditStatus('Save writes this value to the controller.', 'muted');
    } else {
      setMenuEditStatus('UI-only edit. Save stores the value locally in this browser session.', 'muted');
    }
    populateMenuEditForm();

    const backdrop = document.getElementById('menuEditModalBackdrop');
    backdrop.classList.add('open');
    backdrop.setAttribute('aria-hidden', 'false');
    focusMenuEditField();

    if (!menuNodeSupportsRemoteRead(node)) {
      return;
    }

    menuEditState.busy = true;
    setMenuEditStatus('Loading current value from controller...', 'muted');
    syncMenuEditBusyState();

    try {
      const payload = await fetchMenuNodeValue(node, { refresh: true });
      if (!menuEditState.open || menuEditState.nodePath !== node.path) {
        return;
      }

      menuEditState.draftValue = payload.value;
      populateMenuEditForm();
      updateMenuEditCurrentText();
      setMenuEditStatus('Current controller value loaded.', 'muted');
    } catch (error) {
      if (!menuEditState.open || menuEditState.nodePath !== node.path) {
        return;
      }

      setMenuEditStatus('Read failed: ' + error.message, 'err');
    } finally {
      if (menuEditState.open && menuEditState.nodePath === node.path) {
        menuEditState.busy = false;
        syncMenuEditBusyState();
        focusMenuEditField();
      }
    }
  }

  async function saveMenuEdit() {
    const node = currentMenuEditNode();
    const editor = menuEditState.editor;
    if (!node || !editor || menuEditState.busy) {
      return;
    }

    let nextValue = menuEditState.draftValue;

    if (editor.type === 'enum' && !isChoiceButtonEditor(editor)) {
      const raw = document.getElementById('menuEditSelectInput').value;
      const selected = editor.options.find((option) => String(option.value) === raw);
      nextValue = selected ? selected.value : editor.options[0]?.value;
    } else if (editor.type === 'integer' || editor.type === 'float') {
      const raw = document.getElementById('menuEditNumberInput').value.trim();
      if (!raw) {
        setMenuEditStatus('Enter a value first.', 'err');
        return;
      }

      nextValue = editor.type === 'float' ? Number.parseFloat(raw) : Number.parseInt(raw, 10);
      if (!Number.isFinite(nextValue)) {
        setMenuEditStatus('Enter a valid number.', 'err');
        return;
      }
    } else if (nextValue === null || nextValue === undefined) {
      setMenuEditStatus('Choose a value first.', 'err');
      return;
    }

    if (!menuNodeSupportsRemoteWrite(node)) {
      menuValueStore.set(node.path, nextValue);
      closeMenuEditModal();
      renderMenuWidget();
      document.getElementById('menuScreen').focus();
      return;
    }

    menuEditState.busy = true;
    setMenuEditStatus('Saving...', 'muted');
    syncMenuEditBusyState();

    try {
      const payload = await saveMenuNodeValue(node, nextValue);
      menuValueStore.set(node.path, payload.value);
    } catch (error) {
      setMenuEditStatus('Write failed: ' + error.message, 'err');
      menuEditState.busy = false;
      syncMenuEditBusyState();
      return;
    }

    if (isMenuRuleDriverPath(node.path)) {
      rebuildRuntimeMenuTree();
    }
    closeMenuEditModal();
    renderMenuWidget();
    document.getElementById('menuScreen').focus();
    if (typeof refreshPage === 'function') {
      await refreshPage();
    }
  }

  async function handleDashboardRefresh(payload) {
    syncMenuCacheFromDashboard(payload);
    await refreshMenuRuleDrivers();
    rebuildRuntimeMenuTree();
    renderMenuWidget();
    if (!menuEditState.open) {
      await refreshVisibleMenuLeafValues(true);
    }
  }

  function bindEventListeners() {
    if (listenersBound) {
      return;
    }

    document.getElementById('menuEditModalBackdrop').addEventListener('click', (event) => {
      if (event.target.id === 'menuEditModalBackdrop') {
        closeMenuEditModal();
      }
    });
    document.getElementById('menuBackBtn').addEventListener('click', goBackInMenu);
    document.getElementById('menuHomeBtn').addEventListener('click', goHomeInMenu);
    document.getElementById('menuPagePrevBtn').addEventListener('click', () => openSiblingMenu('prev'));
    document.getElementById('menuPageNextBtn').addEventListener('click', () => openSiblingMenu('next'));
    document.getElementById('menuFontSizeRange').addEventListener('input', (event) => {
      updateMenuDisplaySetting({ sizePercent: Number(event.target.value) });
    });
    document.getElementById('menuFontWidthRange').addEventListener('input', (event) => {
      updateMenuDisplaySetting({ widthPercent: Number(event.target.value) });
    });
    document.getElementById('menuFontFamilySelect').addEventListener('change', (event) => {
      updateMenuDisplaySetting({ fontFamily: event.target.value });
    });
    document.getElementById('resetMenuDisplayBtn').addEventListener('click', resetMenuDisplaySettings);
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
    window.addEventListener('resize', applyMenuDisplaySettings);
    listenersBound = true;
  }

  function init(options = {}) {
    refreshPage = typeof options.refreshPage === 'function' ? options.refreshPage : null;
    menuLongPressDurationMs = clampNumber(
      options.longPressDurationMs,
      100,
      3000,
      defaultMenuLongPressDurationMs
    );
    bindEventListeners();
    initializeMenuDisplaySettings();
    initializeMenuWidget();
  }

  return {
    init,
    handleDashboardRefresh,
    __testing: {
      navigateToMenu,
      moveMenuSelection,
      setCurrentMenuPath(path) {
        const menuNode = getMenuNode(path);
        menuCurrentPath = menuNode.path;
        menuSelectedIndex = findSelectableChildIndex(menuNode, null);
        persistMenuLocation();
        renderMenuWidget();
      },
      getCurrentMenuPath() {
        return menuCurrentPath;
      },
      getSelectedIndex() {
        return menuSelectedIndex;
      },
      getStoredValue(path) {
        return menuValueStore.get(path);
      },
      getCurrentMenuChildPaths() {
        return getCurrentMenuChildren().map((child) => child.path);
      },
      getLeafEditor(path) {
        return getLeafEditor(getMenuNode(path));
      },
      getNode(path) {
        return getMenuNode(path);
      }
    }
  };
})();
