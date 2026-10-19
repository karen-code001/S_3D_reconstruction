// Exercise the real app.js upload flow with browser APIs replaced by small fakes.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

async function run(plant) {
  const elements = new Map();
  function element() { return {value:'', disabled:false, hidden:false, style:{}, classList:{add(){},remove(){}}, setAttribute(){}, addEventListener(){}, append(){}, querySelector(){return element();}, focus(){}}; }
  const saved = new Map();
  const requests = [];
  const context = {
    document:{getElementById(id){if(!elements.has(id))elements.set(id,element());return elements.get(id);},createElement:element,addEventListener(){}},
    localStorage:{getItem:k=>saved.get(k)||null,setItem:(k,v)=>saved.set(k,v),removeItem:k=>saved.delete(k)},
    setInterval(){},clearInterval(){},setTimeout(){},clearTimeout(){},
    navigator:{clipboard:{writeText:async()=>{}}},
    fetch:async(url, options={})=>{requests.push({url,options});return {ok:true,headers:{get:()=> 'application/json'},json:async()=>options.method==='POST'?{task_id:'task',upload_url:'http://transfer.test/upload'}:[]};},
    FormData:class {append(){}},
    XMLHttpRequest:class {constructor(){this.upload={};this.status=202;this.responseText='{}';}open(){}send(){this.onload();}},
    console,
  };
  context.window=context;
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../web_UI/app.js'),'utf8'),context);
  const get=id=>elements.get(id);
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
(async()=>{for(const crop of ['tomato','eggplant','pepper']) await run(crop);console.log('Plant selection UI: 3 crops passed');})().catch(e=>{console.error(e);process.exitCode=1;});
