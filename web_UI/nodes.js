(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const els = Object.fromEntries(['nodeSummary','transferNodes','computeSummary','computeNodes','toast'].map(id => [id, $(id)]));
  let toastTimer = null;
  const base = () => (JSON.parse(localStorage.getItem('recon-api-config') || '{}').base || '').replace(/\/$/, '');
  const messageFrom = data => data?.detail || data?.message || data?.error || '请求失败，请检查接口设置。';
  const showToast = (msg, error=false) => { clearTimeout(toastTimer); els.toast.textContent=msg; els.toast.className='toast show'+(error?' error':''); toastTimer=setTimeout(()=>els.toast.className='toast',3200); };
  const requestJson = async (url, options={}) => { const res=await fetch(url,options); const type=res.headers.get('content-type')||''; const data=type.includes('json')?await res.json():await res.text(); if(!res.ok) throw Object.assign(new Error(typeof data==='string'?data:messageFrom(data)),{status:res.status}); return data; };
  const formatAge = s => s<60?s+' 秒前':s<3600?Math.floor(s/60)+' 分钟前':Math.floor(s/3600)+' 小时前';
  // Shared with plants.html, so one admin password per tab covers both pages.
  const ADMIN_KEY_STORAGE = 'recon-admin-key';
  const adminKey = () => { try { return sessionStorage.getItem(ADMIN_KEY_STORAGE) || ''; } catch { return ''; } };
  const togglePause = async node => {
    let key = adminKey() || (prompt('请输入管理口令（与作物管理相同）') || '').trim();
    if (!key) return;
    const resume = node.paused;
    if (!resume && !confirm(`暂停中转节点 ${node.node_id}？\n暂停后不再分配新任务，已接收的任务会照常完成。`)) return;
    try {
      const res = await fetch(base() + '/api/v1/admin/transfer-nodes/' + encodeURIComponent(node.node_id) + '/pause', {method: resume ? 'DELETE' : 'PUT', headers: {'X-Admin-Key': key}});
      if (res.status === 401) { try { sessionStorage.removeItem(ADMIN_KEY_STORAGE); } catch {} throw new Error('管理口令不正确'); }
      if (!res.ok) throw new Error(messageFrom(await res.json().catch(() => null)));
      try { sessionStorage.setItem(ADMIN_KEY_STORAGE, key); } catch {}
      showToast((resume ? '已恢复 ' : '已暂停 ') + node.node_id);
      loadNodes();
    } catch (e) { showToast(e.message, true); }
  };
  const loadNodes = async () => { try{const nodes=await requestJson(base()+'/api/v1/transfer-nodes'); els.nodeSummary.textContent=nodes.filter(n=>n.online).length+' / '+nodes.length+' 在线'; els.transferNodes.innerHTML=nodes.length?'':'<p class="muted">暂无中转节点</p>'; nodes.forEach(n=>{ const row=document.createElement('div'); row.className='recent-item node-item'; const dot=document.createElement('i'); dot.className='node-dot '+(n.assignable?'ok':n.online?'busy':'off'); dot.title=n.paused?'已暂停，不分配新任务':n.assignable?'可分配任务':n.online?'在线，暂不分配':'离线'; const info=document.createElement('div'); info.className='node-info'; const name=document.createElement('strong'); name.textContent=n.node_id; name.title=n.node_id; const meta=document.createElement('span'); meta.textContent=(n.paused?'已暂停 · ':'')+'上传 '+n.current_uploads+'/'+n.max_uploads+' · 磁盘 '+n.disk_usage_percent+'% · '+formatAge(n.seconds_since_heartbeat); info.append(name,meta); const toggle=document.createElement('button'); toggle.type='button'; toggle.className='node-pause'; toggle.textContent=n.paused?'恢复':'暂停'; toggle.title=n.paused?'恢复后重新分配新任务':'暂停后不再分配新任务'; toggle.onclick=()=>togglePause(n); row.append(dot,info,toggle); els.transferNodes.append(row); }); }catch(e){els.nodeSummary.textContent='';els.transferNodes.innerHTML='<p class="muted">无法获取节点状态</p>';} };
  const COMPUTE_REASONS = {not_accepting_tasks:'不接单',at_capacity:'已满',unhealthy_response:'状态异常'};
  const computeTransferFilter = $('computeTransferFilter');
  let selectedTransfer = null;
  let computeGroups = [];
  const showComputeGroup = () => {
    computeGroups.forEach(({transfer, element}) => {
      element.hidden = selectedTransfer !== '' && transfer !== selectedTransfer;
    });
  };
  computeTransferFilter.onchange = () => {
    selectedTransfer = computeTransferFilter.value;
    showComputeGroup();
  };
  els.transferNodes.addEventListener('click', event => {
    if (event.target.closest('button')) return;
    const row = event.target.closest('.node-item');
    const transfer = row?.querySelector('.node-info strong')?.textContent;
    if (!transfer) return;
    if (!computeGroups.some(group => group.transfer === transfer)) {
      showToast('该中转节点下暂无计算节点');
      return;
    }
    selectedTransfer = transfer;
    computeTransferFilter.value = transfer;
    showComputeGroup();
  });
  const loadComputeNodes = async () => {
    try {
      const nodes = await requestJson(base() + '/api/v1/compute-nodes');
      els.computeSummary.textContent = nodes.filter(n => n.available).length + ' / ' + nodes.length + ' 可用';
      els.computeNodes.innerHTML = nodes.length ? '' : '<p class="muted">暂无计算节点</p>';
      const groups = new Map();
      nodes.forEach(node => {
        const transfer = node.transfer_node_id || '未指定中转节点';
        if (!groups.has(transfer)) groups.set(transfer, []);
        groups.get(transfer).push(node);
      });
      computeTransferFilter.innerHTML = '';
      computeTransferFilter.disabled = groups.size === 0;
      if (selectedTransfer !== '' && !groups.has(selectedTransfer)) selectedTransfer = groups.keys().next().value || '';
      if (groups.size) {
        const option = document.createElement('option');
        option.value = '';
        option.textContent = '全部';
        computeTransferFilter.append(option);
      }
      if (!groups.size) {
        const option = document.createElement('option');
        option.value = '';
        option.textContent = '暂无中转节点分组';
        computeTransferFilter.append(option);
      }
      computeGroups = [];
      groups.forEach((members, transfer) => {
        const option = document.createElement('option');
        option.value = transfer;
        option.textContent = transfer;
        computeTransferFilter.append(option);
        const group = document.createElement('section');
        group.className = 'compute-group';
        computeGroups.push({transfer, element: group});
        const heading = document.createElement('div');
        heading.className = 'compute-group-heading';
        const title = document.createElement('h3');
        title.textContent = transfer;
        const summary = document.createElement('span');
        summary.className = 'muted';
        summary.textContent = members.filter(n => n.available).length + ' / ' + members.length + ' 可用';
        heading.append(title, summary);
        group.append(heading);
        // Keep unreachable nodes in their own transfer group, after reachable nodes.
        const ordered = [...members.filter(n => n.reachable), ...members.filter(n => !n.reachable)];
        ordered.forEach(node => {
          const row = document.createElement('div');
          row.className = 'recent-item node-item';
          const dot = document.createElement('i');
          dot.className = 'node-dot ' + (!node.reachable ? 'off' : node.available ? 'ok' : 'busy');
          dot.title = !node.reachable ? '未连通' : node.available ? '可接收任务' : (COMPUTE_REASONS[node.reason] || '暂不可用');
          const info = document.createElement('div');
          info.className = 'node-info';
          const name = document.createElement('strong');
          name.textContent = node.node_id;
          name.title = node.node_id;
          const meta = document.createElement('span');
          const details = [];
          if (node.reachable && node.capacity != null) details.push('任务 ' + node.active_jobs + '/' + node.capacity);
          details.push('经 ' + transfer);
          if (!node.reachable) details.push('未连通');
          else if (!node.available) details.push(COMPUTE_REASONS[node.reason] || '暂不可用');
          meta.textContent = details.join(' · ');
          info.append(name, meta);
          row.append(dot, info);
          group.append(row);
        });
        els.computeNodes.append(group);
      });
      computeTransferFilter.value = selectedTransfer;
      showComputeGroup();
    } catch (e) {
      els.computeSummary.textContent = '';
      els.computeNodes.innerHTML = '<p class="muted">无法获取节点状态</p>';
    }
  };
  loadNodes();
  setInterval(loadNodes,5000);
  loadComputeNodes();
  setInterval(loadComputeNodes,10000);
})();
