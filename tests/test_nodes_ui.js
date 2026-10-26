// Run the real nodes.js against a merged /api/v1/compute-nodes response with small DOM fakes.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

function element(tag = 'div') {
  return {
    tag, children: [], hidden: false, className: '', textContent: '', value: '', disabled: false, title: '',
    set innerHTML(html) { this.children = []; this.html = html; },
    get innerHTML() { return this.html || ''; },
    append(...items) { this.children.push(...items.filter(item => typeof item === 'object')); },
    addEventListener() {}, querySelector() { return null; },
  };
}

const COMPUTE = [
  // Reached through both transfer nodes; busy behind ZBY's tunnel.
  {node_id: 'Server-1', transfer_node_id: '3090C', reachable: true, available: true, active_jobs: 0, capacity: 1,
   paths: [{transfer_node_id: '3090C', reachable: true, available: true}, {transfer_node_id: 'ZBY', reachable: false, available: false, reason: 'unreachable_or_invalid_response'}]},
  {node_id: 'Server-2', transfer_node_id: '3090C', reachable: true, available: false, active_jobs: 1, capacity: 1, reason: 'at_capacity',
   paths: [{transfer_node_id: '3090C', reachable: true, available: false, reason: 'at_capacity'}]},
];

(async () => {
  const elements = new Map();
  const context = {
    document: {getElementById: id => { if (!elements.has(id)) elements.set(id, element()); return elements.get(id); }, createElement: element},
    localStorage: {getItem: () => null}, sessionStorage: {getItem: () => null, setItem() {}},
    setInterval() {}, setTimeout() {}, clearTimeout() {},
    fetch: async url => ({ok: true, headers: {get: () => 'application/json'},
      json: async () => url.endsWith('/api/v1/compute-nodes') ? COMPUTE : []}),
    console,
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../web_UI/nodes.js'), 'utf8'), context);
  await new Promise(resolve => setImmediate(resolve));

  const get = id => elements.get(id);
  assert.equal(get('computeSummary').textContent, '1 / 2 可用', 'summary counts each compute node once');
  const groups = get('computeNodes').children.map(group => ({
    transfer: group.children[0].children[0].textContent,
    rows: group.children.slice(1).map(row => row.children[1].children[0].textContent + ':' + row.children[0].className),
  }));
  assert.deepEqual(groups, [
    {transfer: '3090C', rows: ['Server-1:node-dot ok', 'Server-2:node-dot busy']},
    {transfer: 'ZBY', rows: ['Server-1:node-dot off']},
  ]);
  assert.deepEqual(get('computeTransferFilter').children.map(o => o.value), ['', '3090C', 'ZBY']);
  console.log('Nodes UI: merged compute nodes listed under every transfer path');
})().catch(e => { console.error(e); process.exitCode = 1; });
