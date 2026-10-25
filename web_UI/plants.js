(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const KEY_STORAGE = 'recon-admin-key';
  const ERRORS = {
    'plant key already exists': '代号已存在',
    'plant not found': '作物不存在，请刷新页面',
    'plant is used by existing tasks; disable it instead': '已有任务使用这个作物，只能停用，不能删除',
  };
  // Same API base as the task page (its "接口设置"); empty means this site.
  const base = () => { try { return (JSON.parse(localStorage.getItem('recon-api-config') || '{}').base || '').replace(/\/$/, ''); } catch { return ''; } };
  const session = { get: () => { try { return sessionStorage.getItem(KEY_STORAGE) || ''; } catch { return ''; } }, set: v => { try { sessionStorage.setItem(KEY_STORAGE, v); } catch { /* private mode */ } } };

  let toastTimer = null;
  const showToast = (message, error = false) => { clearTimeout(toastTimer); $('toast').textContent = message; $('toast').className = 'toast show' + (error ? ' error' : ''); toastTimer = setTimeout(() => { $('toast').className = 'toast'; }, 3600); };

  const errorText = (status, data) => {
    if (status === 401) return '管理口令不正确';
    const detail = data && data.detail;
    if (typeof detail === 'string') return ERRORS[detail] || detail;
    if (Array.isArray(detail)) return detail.map(item => String(item.msg || '').replace(/^Value error, /, '')).join('；');
    return `请求失败（HTTP ${status}）`;
  };
  const request = async (path, options = {}) => {
    const headers = { 'X-Admin-Key': $('adminKey').value.trim(), ...(options.body ? { 'Content-Type': 'application/json' } : {}) };
    const response = await fetch(base() + path, { ...options, headers });
    if (response.status === 204) return null;
    const data = await response.json().catch(() => null);
    if (!response.ok) throw Object.assign(new Error(errorText(response.status, data)), { status: response.status });
    return data;
  };

  // name -> { size, nodes: [node ids] } across reachable compute nodes.
  let iniFiles = new Map();
  let reachableNodes = 0;
  const iniState = (name, segment) => {
    if (!name) return ['', ''];
    if (!reachableNodes) return ['', '无法确认文件是否存在（没有可查询的计算节点）'];
    const file = iniFiles.get(name);
    if (!file) return ['bad', '计算节点上找不到这个文件'];
    if (file.nodes.length < reachableNodes) return ['warn', `只有 ${file.nodes.length}/${reachableNodes} 个计算节点有这个文件`];
    if (file.size === 0) return segment ? ['bad', '文件是空的，分割模式会被计算节点拒绝'] : ['ok', '文件是空的（仅重建模式不读取，可以使用）'];
    return ['ok', '计算节点上已找到'];
  };
  const showIniState = (element, name, segment) => { const [level, text] = iniState(name, segment); element.className = 'ini-state' + (level ? ' ' + level : ''); element.textContent = text; };

  const input = (value, attrs = {}) => { const el = document.createElement('input'); el.value = value; Object.assign(el, attrs); return el; };
  const cell = (...children) => { const td = document.createElement('td'); td.append(...children); return td; };
  const modeSelect = segment => {
    const select = document.createElement('select');
    [['true', '分割'], ['false', '仅重建不分割']].forEach(([value, label]) => { const option = document.createElement('option'); option.value = value; option.textContent = label; select.append(option); });
    select.value = String(segment);
    return select;
  };

  const renderRows = plants => {
    const body = $('plantRows');
    body.replaceChildren();
    plants.forEach(plant => {
      const row = document.createElement('tr');
      const fields = {
        name_zh: input(plant.name_zh, { maxLength: 50 }),
        name_en: input(plant.name_en, { maxLength: 80 }),
        yolo_ini: input(plant.yolo_ini),
        segment: modeSelect(plant.segment),
        enabled: input('', { type: 'checkbox', checked: plant.enabled }),
        sort_order: input(String(plant.sort_order), { type: 'number' }),
      };
      fields.yolo_ini.setAttribute('list', 'iniOptions');
      const state = document.createElement('span');
      const save = document.createElement('button');
      save.className = 'row-button save'; save.type = 'button'; save.textContent = '保存'; save.disabled = true;
      const remove = document.createElement('button');
      remove.className = 'row-button danger'; remove.type = 'button'; remove.textContent = '删除';
      const code = document.createElement('code'); code.textContent = plant.key;

      const values = () => ({
        name_zh: fields.name_zh.value.trim(),
        name_en: fields.name_en.value.trim(),
        yolo_ini: fields.yolo_ini.value.trim(),
        segment: fields.segment.value === 'true',
        enabled: fields.enabled.checked,
        sort_order: Number(fields.sort_order.value) || 0,
      });
      const changes = () => Object.fromEntries(Object.entries(values()).filter(([key, value]) => value !== plant[key]));
      const refresh = () => {
        const dirty = Object.keys(changes()).length > 0;
        row.classList.toggle('dirty', dirty);
        row.classList.toggle('disabled', !fields.enabled.checked);
        save.disabled = !dirty;
        showIniState(state, fields.yolo_ini.value.trim(), fields.segment.value === 'true');
      };
      Object.values(fields).forEach(el => { el.addEventListener('input', refresh); el.addEventListener('change', refresh); });
      row.refreshIni = refresh;

      save.onclick = async () => {
        save.disabled = true;
        try {
          Object.assign(plant, await request('/api/v1/admin/plants/' + encodeURIComponent(plant.key), { method: 'PUT', body: JSON.stringify(changes()) }));
          showToast(`已保存「${plant.name_zh}」，新任务立即生效`);
          refresh();
        } catch (e) { showToast(e.message, true); refresh(); }
      };
      remove.onclick = async () => {
        if (!confirm(`确定删除「${plant.name_zh}」(${plant.key})？`)) return;
        try {
          await request('/api/v1/admin/plants/' + encodeURIComponent(plant.key), { method: 'DELETE' });
          showToast(`已删除「${plant.name_zh}」`);
          loadPlants();
        } catch (e) { showToast(e.message, true); }
      };

      const actions = cell(save, ' ', remove);
      actions.className = 'col-actions';
      const enabledCell = cell(fields.enabled);
      enabledCell.className = 'col-enabled';
      row.append(cell(code), cell(fields.name_zh), cell(fields.name_en), cell(fields.yolo_ini, state), cell(fields.segment), enabledCell, cell(fields.sort_order), actions);
      body.append(row);
      refresh();
    });
  };

  const loadPlants = async () => {
    try {
      const response = await fetch(base() + '/api/v1/plants');
      if (!response.ok) throw new Error(`读取作物列表失败（HTTP ${response.status}）`);
      renderRows(await response.json());
    } catch (e) { showToast(e.message, true); }
  };

  const refreshIniStates = () => {
    document.querySelectorAll('#plantRows tr').forEach(row => row.refreshIni && row.refreshIni());
    showIniState($('newIniState'), $('newIni').value.trim(), $('newSegment').value === 'true');
  };
  const loadInis = async () => {
    if (!$('adminKey').value.trim()) { $('nodesLine').textContent = '输入管理口令后，可以显示计算节点上已有的 ini 文件。'; return; }
    try {
      const nodes = await request('/api/v1/admin/yolo-inis');
      const reachable = nodes.filter(node => node.reachable && !node.error);
      reachableNodes = reachable.length;
      iniFiles = new Map();
      reachable.forEach(node => node.files.forEach(file => {
        const entry = iniFiles.get(file.name) || { size: file.size, nodes: [] };
        entry.nodes.push(node.node_id);
        entry.size = Math.min(entry.size, file.size);
        iniFiles.set(file.name, entry);
      }));
      const list = $('iniOptions');
      list.replaceChildren(...[...iniFiles.keys()].sort().map(name => { const option = document.createElement('option'); option.value = name; return option; }));
      const offline = nodes.filter(node => !node.reachable || node.error).map(node => node.node_id + (node.error ? `（${node.error}）` : '（离线）'));
      $('nodesLine').textContent = nodes.length
        ? `计算节点 ${reachableNodes}/${nodes.length} 可查询，共 ${iniFiles.size} 个 ini 文件` + (offline.length ? `；无法查询：${offline.join('、')}` : '')
        : '当前没有在线的中转节点，无法读取计算节点上的 ini 文件。';
    } catch (e) {
      reachableNodes = 0;
      $('nodesLine').textContent = e.status === 401 ? '管理口令不正确，无法读取 ini 文件列表。' : '读取 ini 文件列表失败：' + e.message;
    }
    refreshIniStates();
  };

  $('checkKey').onclick = async () => {
    try {
      await request('/api/v1/admin/check');
      session.set($('adminKey').value.trim());
      showToast('管理口令正确');
      loadInis();
    } catch (e) { showToast(e.message, true); }
  };
  $('adminKey').onkeydown = e => { if (e.key === 'Enter') $('checkKey').onclick(); };
  ['newIni', 'newSegment'].forEach(id => { $(id).addEventListener('input', refreshIniStates); $(id).addEventListener('change', refreshIniStates); });
  $('addPlant').onclick = async () => {
    const body = {
      key: $('newKey').value.trim(),
      name_zh: $('newNameZh').value.trim(),
      name_en: $('newNameEn').value.trim(),
      yolo_ini: $('newIni').value.trim(),
      segment: $('newSegment').value === 'true',
      enabled: $('newEnabled').checked,
    };
    if ($('newOrder').value.trim()) body.sort_order = Number($('newOrder').value);
    if (!body.key || !body.name_zh || !body.yolo_ini) { showToast('请填写代号、中文名和 YOLO ini', true); return; }
    try {
      await request('/api/v1/admin/plants', { method: 'POST', body: JSON.stringify(body) });
      showToast(`已添加「${body.name_zh}」`);
      ['newKey', 'newNameZh', 'newNameEn', 'newIni', 'newOrder'].forEach(id => { $(id).value = ''; });
      refreshIniStates();
      loadPlants();
    } catch (e) { showToast(e.message, true); }
  };

  $('adminKey').value = session.get();
  loadPlants().then(loadInis);
})();
