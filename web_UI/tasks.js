(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const KEY_STORAGE = 'recon-admin-key';  // shared with plants.html
  const base = () => { try { return (JSON.parse(localStorage.getItem('recon-api-config') || '{}').base || '').replace(/\/$/, ''); } catch { return ''; } };
  const session = { get: () => { try { return sessionStorage.getItem(KEY_STORAGE) || ''; } catch { return ''; } }, set: v => { try { sessionStorage.setItem(KEY_STORAGE, v); } catch { /* private mode */ } } };

  let toastTimer = null;
  const showToast = (message, error = false) => { clearTimeout(toastTimer); $('toast').textContent = message; $('toast').className = 'toast show' + (error ? ' error' : ''); toastTimer = setTimeout(() => { $('toast').className = 'toast'; }, 3600); };
  const request = async (path, options = {}) => {
    const headers = { 'X-Admin-Key': $('adminKey').value.trim(), ...(options.body ? { 'Content-Type': 'application/json' } : {}) };
    const response = await fetch(base() + path, { ...options, headers });
    const data = await response.json().catch(() => null);
    if (!response.ok) {
      const detail = data && data.detail;
      throw new Error(response.status === 401 ? '管理口令不正确'
        : typeof detail === 'string' ? detail
        : Array.isArray(detail) ? detail.map(item => String(item.msg || '').replace(/^Value error, /, '')).join('；')
        : `请求失败（HTTP ${response.status}）`);
    }
    return data;
  };

  // All times on this page are Beijing time (UTC+8, no DST), whatever the browser's own timezone.
  // Inputs hold Beijing wall time as datetime-local values ("2026-10-25T18:00"); '' means open-ended.
  const BEIJING_OFFSET_MINUTES = 480;
  const EARLIEST = '1970-01-01T00:00:00+00:00', LATEST = '2100-01-01T00:00:00+00:00';
  const pad = n => String(n).padStart(2, '0');
  // Shift by +8 h and read the UTC fields, so the browser's timezone never enters.
  const beijingValue = ms => {
    const t = new Date(ms + BEIJING_OFFSET_MINUTES * 60000);
    return `${t.getUTCFullYear()}-${pad(t.getUTCMonth() + 1)}-${pad(t.getUTCDate())}T${pad(t.getUTCHours())}:${pad(t.getUTCMinutes())}`;
  };
  const toIso = value => {
    const [date, time = '00:00'] = value.split('T');
    return `${date}T${time.slice(0, 5)}:00+08:00`;
  };
  const dayRange = day => {
    const [y, m, d] = day.split('-').map(Number);
    const next = new Date(Date.UTC(y, m - 1, d + 1));  // calendar arithmetic only, no timezone involved
    return { from: `${day}T00:00`, to: `${next.getUTCFullYear()}-${pad(next.getUTCMonth() + 1)}-${pad(next.getUTCDate())}T00:00` };
  };
  const range = ({ from, to }) => ({ start: from ? toIso(from) : EARLIEST, end: to ? toIso(to) : LATEST });
  const show = value => value.replace('T', ' ');
  const rangeText = ({ from, to }) => !from && !to ? '全部时间' : !from ? `${show(to)} 之前` : !to ? `${show(from)} 及以后` : `${show(from)} 至 ${show(to)}（不含）`;
  const selected = () => {
    const sel = { from: $('fromTime').value, to: $('toTime').value };
    if (sel.from && sel.to && sel.to <= sel.from) throw new Error('截止时间必须晚于起始时间');
    return sel;
  };
  const resetActions = () => { $('hideRange').disabled = $('showRange').disabled = true; };

  const preview = async () => {
    try {
      const sel = selected();
      const r = await request('/api/v1/admin/tasks/visibility/preview', { method: 'POST', body: JSON.stringify(range(sel)) });
      $('previewLine').textContent = `${rangeText(sel)}：共 ${r.total} 个任务，其中 ${r.hidden} 个已隐藏，${r.total - r.hidden} 个在 8088 显示。`
        + (r.future ? ' 截止时间在未来：隐藏后，这段时间内之后提交的任务也会自动隐藏。' : '')
        + (r.rules ? ` 有 ${r.rules} 条生效中的隐藏规则与这段时间重叠，恢复显示会同时删除它们。` : '');
      // A future range is worth hiding even when it holds no task yet; restoring also clears its rules.
      $('hideRange').disabled = r.total === r.hidden && !r.future;
      $('showRange').disabled = r.hidden === 0 && r.rules === 0;
    } catch (e) { showToast(e.message, true); }
  };

  const apply = async (sel, hidden) => {
    const action = hidden ? '隐藏' : '恢复显示';
    if (!confirm(`${action}「${rangeText(sel)}」提交的全部任务？`)) return;
    try {
      const r = await request('/api/v1/admin/tasks/visibility', { method: 'POST', body: JSON.stringify({ ...range(sel), hidden }) });
      showToast(`已${action} ${r.updated} 个任务（${rangeText(sel)} 共 ${r.total} 个）`
        + (hidden && r.future ? '，之后在这段时间内提交的任务也会自动隐藏' : ''));
      await Promise.all([loadDays(), loadRules()]);
      await preview();
    } catch (e) { showToast(e.message, true); }
  };

  // Server times (UTC) as Beijing wall time; the far bounds the page sends for empty inputs read as open ends.
  const ruleTime = (iso, openText) => {
    const ms = Date.parse(iso);
    return ms <= Date.parse(EARLIEST) || ms >= Date.parse(LATEST) ? openText : show(beijingValue(ms));
  };
  const loadRules = async () => {
    const body = $('ruleRows');
    if (!$('adminKey').value.trim()) return;
    try {
      const rules = await request('/api/v1/admin/tasks/hide-rules');
      body.replaceChildren(...rules.map(rule => {
        const row = document.createElement('tr');
        const remove = button('删除规则', 'danger', async () => {
          if (!confirm('删除这条规则？之后在这段时间内提交的任务会正常显示，已隐藏的任务不受影响。')) return;
          try {
            await request('/api/v1/admin/tasks/hide-rules/' + rule.id, { method: 'DELETE' });
            showToast('已删除隐藏规则');
            loadRules();
          } catch (e) { showToast(e.message, true); }
        });
        const actions = cell(remove);
        actions.className = 'col-actions';
        row.append(cell(ruleTime(rule.start, '最早')), cell(ruleTime(rule.end, '长期有效')), actions);
        return row;
      }));
      if (!rules.length) body.innerHTML = '<tr><td colspan="3" class="muted">没有生效中的规则。</td></tr>';
    } catch (e) {
      body.innerHTML = '<tr><td colspan="3" class="muted"></td></tr>';
      body.querySelector('td').textContent = e.message;
    }
  };

  const cell = (...children) => { const td = document.createElement('td'); td.append(...children); return td; };
  const button = (text, cls, onClick) => { const b = document.createElement('button'); b.type = 'button'; b.className = 'row-button ' + cls; b.textContent = text; b.onclick = onClick; return b; };
  const loadDays = async () => {
    const body = $('dayRows');
    if (!$('adminKey').value.trim()) return;
    try {
      const days = await request('/api/v1/admin/tasks/daily?tz_offset_minutes=' + BEIJING_OFFSET_MINUTES);
      body.replaceChildren(...days.map(day => {
        const row = document.createElement('tr');
        if (day.hidden === day.total) row.className = 'disabled';
        const date = document.createElement('a');
        date.href = '#'; date.textContent = day.date; date.className = 'day-link';
        // Day buttons also fill the range above, so the summary after the change describes that day.
        const pick = () => { const r = dayRange(day.date); $('fromTime').value = r.from; $('toTime').value = r.to; return r; };
        date.onclick = e => { e.preventDefault(); pick(); preview(); };
        const state = day.hidden === 0 ? '全部显示' : day.hidden === day.total ? '全部隐藏' : `${day.total - day.hidden} 个显示`;
        const actions = cell(
          button('隐藏', 'danger', () => apply(pick(), true)), ' ',
          button('恢复', 'save', () => apply(pick(), false)),
        );
        actions.className = 'col-actions';
        actions.firstChild.disabled = day.hidden === day.total;
        actions.lastChild.disabled = day.hidden === 0;
        row.append(cell(date), cell(String(day.total)), cell(String(day.hidden)), cell(state), actions);
        return row;
      }));
      if (!days.length) body.innerHTML = '<tr><td colspan="5" class="muted">还没有任务。</td></tr>';
    } catch (e) {
      body.innerHTML = '<tr><td colspan="5" class="muted"></td></tr>';
      body.querySelector('td').textContent = e.message;
    }
  };

  $('checkKey').onclick = async () => {
    try {
      await request('/api/v1/admin/check');
      session.set($('adminKey').value.trim());
      showToast('管理口令正确');
      loadDays(); loadRules();
    } catch (e) { showToast(e.message, true); }
  };
  $('adminKey').onkeydown = e => { if (e.key === 'Enter') $('checkKey').onclick(); };
  $('preview').onclick = preview;
  ['fromTime', 'toTime'].forEach(id => { $(id).oninput = resetActions; });
  $('toNow').onclick = () => { $('toTime').value = beijingValue(Date.now()); resetActions(); };
  $('clearRange').onclick = () => { $('fromTime').value = $('toTime').value = ''; resetActions(); };
  $('hideRange').onclick = () => { try { apply(selected(), true); } catch (e) { showToast(e.message, true); } };
  $('showRange').onclick = () => { try { apply(selected(), false); } catch (e) { showToast(e.message, true); } };

  $('adminKey').value = session.get();
  loadDays(); loadRules();
})();
