// Exercise the real app.js upload flow with browser APIs replaced by small fakes.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const PLANTS = [
  {key:'tomato_new',name_zh:'番茄',segment:true,enabled:true},
  {key:'eggplant',name_zh:'茄子',segment:true,enabled:true},
  {key:'maize',name_zh:'玉米',segment:true,enabled:true},
  {key:'other',name_zh:'其他植物',segment:false,enabled:true},
  {key:'tomato',name_zh:'番茄（旧）',segment:true,enabled:false},
];

async function start() {
  const elements = new Map();
  function element() { return {value:'', disabled:false, hidden:false, style:{}, children:[], classList:{add(){},remove(){}}, setAttribute(){}, addEventListener(){}, append(...items){this.children.push(...items);}, set innerHTML(html){this.children=[];}, querySelector(){return element();}, focus(){}}; }
  const saved = new Map();
  const requests = [];
  const context = {
    document:{getElementById(id){if(!elements.has(id))elements.set(id,element());return elements.get(id);},createElement:element,addEventListener(){}},
    localStorage:{getItem:k=>saved.get(k)||null,setItem:(k,v)=>saved.set(k,v),removeItem:k=>saved.delete(k)},
    setInterval(){},clearInterval(){},setTimeout(){},clearTimeout(){},
    navigator:{clipboard:{writeText:async()=>{}}},
    fetch:async(url, options={})=>{requests.push({url,options});const body=options.method==='POST'?{task_id:'task',upload_url:'http://transfer.test/upload'}:url.endsWith('/api/v1/plants')?PLANTS:[];return {ok:true,headers:{get:()=> 'application/json'},json:async()=>body};},
    FormData:class {append(){}},
    XMLHttpRequest:class {constructor(){this.upload={};this.status=202;this.responseText='{}';}open(){}send(){this.onload();}},
    console,
  };
  context.window=context;
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../web_UI/app.js'),'utf8'),context);
  await new Promise(resolve=>setImmediate(resolve));
  return {get:id=>elements.get(id), saved, requests};
}

async function upload(plant) {
  const {get, saved, requests} = await start();
  get('videoInput').onchange({target:{files:[{name:'video.mp4',type:'video/mp4',size:5}]}});
  assert.equal(get('uploadButton').disabled,true,'crop must be selected');
  await get('uploadButton').onclick();
  assert.equal(requests.filter(r=>r.options.method==='POST').length,0,'no request without crop');
  get('plantType').value=plant;
  get('plantType').onchange();
  assert.equal(get('uploadButton').disabled,false);
  await get('uploadButton').onclick();
  const creation=requests.find(r=>r.options.method==='POST');
  assert.equal(JSON.parse(creation.options.body).plant_type,plant);
  assert.equal(JSON.parse(saved.get('recon-recent-tasks'))[0].plant_type,plant);
  assert.equal(get('plantType').disabled,false);
}

(async()=>{
  const html=fs.readFileSync(path.join(__dirname,'../web_UI/index.html'),'utf8');
  assert.equal([...html.matchAll(/<option value="\w+">/g)].length,0,'crops must come from /api/v1/plants, not index.html');
  const {get}=await start();
  const offered=get('plantType').children.map(o=>o.value+'='+o.textContent);
  assert.deepEqual(offered,['tomato_new=番茄','eggplant=茄子','maize=玉米','other=其他植物（仅重建不分割）']);
  for(const crop of ['tomato_new','eggplant','maize','other']) await upload(crop);
  console.log('Plant selection UI: 4 crops from the control plane passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
