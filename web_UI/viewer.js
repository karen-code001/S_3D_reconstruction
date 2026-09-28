(async()=>{
const THREE=await import('three');
const {OrbitControls}=await import('three/addons/controls/OrbitControls.js');
const {PLYLoader}=await import('three/addons/loaders/PLYLoader.js');
const {PCDLoader}=await import('three/addons/loaders/PCDLoader.js');

const canvas=document.getElementById('pointCloudCanvas');
const state=document.getElementById('viewerState');
const taskLabel=document.getElementById('taskLabel');
const fileLabel=document.getElementById('fileLabel');
const pointCount=document.getElementById('pointCount');
const viewerInfo=document.getElementById('viewerInfo');
const pointSize=document.getElementById('pointSize');
const resetView=document.getElementById('resetView');
const pointCloudSection=document.getElementById('pointCloudSection');
const closeViewer=document.getElementById('closeViewer');
const modeButtons=[...document.querySelectorAll('[data-color-mode]')];
const colorLegend=document.getElementById('colorLegend');
const legendTitle=document.getElementById('legendTitle');
const legendSummary=document.getElementById('legendSummary');
const legendItems=document.getElementById('legendItems');

const renderer=new THREE.WebGLRenderer({canvas,antialias:true,alpha:true});
renderer.setPixelRatio(Math.min(window.devicePixelRatio,2));
renderer.setClearColor(0x000000,0);
const scene=new THREE.Scene();
const camera=new THREE.PerspectiveCamera(55,1,0.01,100000);
camera.position.set(3,2,3);
const controls=new OrbitControls(camera,canvas);
controls.enableDamping=true;
controls.dampingFactor=.08;
controls.screenSpacePanning=true;
controls.minDistance=.001;
controls.maxDistance=100000;
let cloud=null;
let initialView=null;
let animationStarted=false;
let activeMode='rgb';

const MODE_CONFIG={rgb:{attribute:'rgbColor'},semantic:{attribute:'semanticColor'},instance:{attribute:'instanceColor'}};
const SEMANTIC_PALETTE=[0x4e79a7,0xf28e2b,0xe15759,0x76b7b2,0x59a14f,0xedc949,0xaf7aa1,0xff9da7,0x9c755f,0xbab0ab,0x5f8dd3,0xffbe63,0xd37295,0x86bcb6,0x8cd17d,0xb6992d,0x8f7aa8,0xf1a2a9,0xa17464,0xc8c8c8];

const showError=message=>{state.className='viewer-state error';state.hidden=false;state.innerHTML='<strong>无法显示点云</strong><p></p>';state.querySelector('p').textContent=message;};
const nextPaint=()=>new Promise(resolve=>requestAnimationFrame(()=>resolve()));
const loadingMarkup=()=>'<div class="loader" aria-hidden="true"></div><strong>正在加载点云结果</strong><p>请稍候，较大的点云文件可能需要一些时间。</p><div class="viewer-progress-list"><div class="viewer-progress-item"><div><span>下载</span><b id="downloadProgressPercent">0%</b></div><div id="downloadProgress" class="viewer-load-progress" role="progressbar" aria-label="点云下载进度" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0"><i id="downloadProgressBar"></i></div></div><div class="viewer-progress-item"><div><span>解析与着色</span><b id="parseProgressPercent">0%</b></div><div id="parseProgress" class="viewer-load-progress" role="progressbar" aria-label="点云解析与着色进度" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0"><i id="parseProgressBar"></i></div></div></div>';
const resize=()=>{const width=canvas.clientWidth,height=canvas.clientHeight;renderer.setSize(width,height,false);camera.aspect=width/Math.max(height,1);camera.updateProjectionMatrix();};
const animate=()=>{resize();controls.update();renderer.render(scene,camera);requestAnimationFrame(animate);};
const pointMaterial=hasColors=>new THREE.PointsMaterial({size:Number(pointSize.value),sizeAttenuation:true,vertexColors:hasColors,color:hasColors?0xffffff:0x57d9d0});

function setStageProgress(stage,value){
  const percent=Math.max(0,Math.min(100,Math.round(value)));
  const progress=document.getElementById(stage+'Progress');
  const bar=document.getElementById(stage+'ProgressBar');
  const label=document.getElementById(stage+'ProgressPercent');
  if(progress)progress.setAttribute('aria-valuenow',String(percent));
  if(bar)bar.style.width=percent+'%';
  if(label)label.textContent=percent+'%';
}

async function fetchPointCloudData(url){
  const response=await fetch(url);
  if(!response.ok)throw new Error('点云文件下载失败：HTTP '+response.status);
  const total=Number(response.headers.get('content-length'))||0;
  if(!response.body){const buffer=await response.arrayBuffer();setStageProgress('download',100);return buffer;}
  const reader=response.body.getReader(),chunks=[];
  let received=0;
  while(true){
    const {done,value}=await reader.read();
    if(done)break;
    chunks.push(value);received+=value.byteLength;
    if(total)setStageProgress('download',received/total*100);
  }
  const bytes=new Uint8Array(received);
  let offset=0;
  for(const chunk of chunks){bytes.set(chunk,offset);offset+=chunk.byteLength;}
  setStageProgress('download',100);
  return bytes.buffer;
}

function normalizeGeometry(geometry){
  geometry.computeBoundingBox();
  const box=geometry.boundingBox;
  if(!box||box.isEmpty())throw new Error('点云文件中没有有效坐标');
  const center=box.getCenter(new THREE.Vector3());
  geometry.translate(-center.x,-center.y,-center.z);
  geometry.computeBoundingSphere();
  return geometry;
}

function hashString(value){
  let hash=2166136261;
  for(const char of String(value)){hash^=char.charCodeAt(0);hash=Math.imul(hash,16777619);}
  return hash>>>0;
}

function colorForClass(value,mode,target=new THREE.Color()){
  if(mode==='semantic')return target.setHex(SEMANTIC_PALETTE[hashString(value)%SEMANTIC_PALETTE.length]);
  const hash=hashString('instance:'+value);
  return target.setHSL((hash*0.618033988749895)%1,.66,.58);
}

function prepareStandardGeometry(geometry){
  const color=geometry.getAttribute('color');
  if(color&&!geometry.getAttribute('rgbColor'))geometry.setAttribute('rgbColor',color);
  geometry.userData.colorModes=geometry.userData.colorModes||{};
  if(geometry.getAttribute('rgbColor'))geometry.userData.colorModes.rgb={available:true,legend:[]};
  return geometry;
}

function availableModes(){
  if(!cloud)return [];
  return Object.entries(MODE_CONFIG).filter(([,config])=>Boolean(cloud.geometry.getAttribute(config.attribute))).map(([mode])=>mode);
}

function renderLegend(mode){
  const entries=cloud?.geometry.userData.colorModes?.[mode]?.legend||[];
  if(mode==='rgb'||!entries.length){colorLegend.hidden=true;legendItems.replaceChildren();return;}
  colorLegend.hidden=false;
  legendTitle.textContent=mode==='semantic'?'语义类别':'实例编号';
  legendSummary.textContent=entries.length.toLocaleString()+(mode==='semantic'?' 类':' 个');
  const fragment=document.createDocumentFragment();
  entries.forEach(entry=>{
    const row=document.createElement('div');row.className='legend-item';
    const swatch=document.createElement('span');swatch.className='legend-swatch';swatch.style.backgroundColor='#'+entry.color.toString(16).padStart(6,'0');
    const name=document.createElement('span');name.className='legend-name';name.textContent=(mode==='semantic'?'类别 ':'实例 ')+entry.value;name.title=name.textContent;
    const count=document.createElement('span');count.className='legend-count';count.textContent=entry.count.toLocaleString();
    row.append(swatch,name,count);fragment.append(row);
  });
  legendItems.replaceChildren(fragment);
}

function setColorMode(mode){
  if(!cloud||!MODE_CONFIG[mode])return;
  const attribute=cloud.geometry.getAttribute(MODE_CONFIG[mode].attribute);
  if(!attribute)return;
  activeMode=mode;
  cloud.geometry.setAttribute('color',attribute);
  cloud.geometry.attributes.color.needsUpdate=true;
  cloud.material.vertexColors=true;
  cloud.material.color.setHex(0xffffff);
  cloud.material.needsUpdate=true;
  modeButtons.forEach(button=>{const selected=button.dataset.colorMode===mode;button.classList.toggle('active',selected);button.setAttribute('aria-pressed',String(selected));});
  renderLegend(mode);
}

function configureModeControls(){
  const modes=availableModes();
  modeButtons.forEach(button=>{button.disabled=!modes.includes(button.dataset.colorMode);});
  const preferred=modes.includes(activeMode)?activeMode:(modes.includes('rgb')?'rgb':modes[0]);
  if(preferred)setColorMode(preferred);
  else{modeButtons.forEach(button=>{button.classList.remove('active');button.setAttribute('aria-pressed','false');});colorLegend.hidden=true;}
}

function installCloud(points,name){
  if(cloud){scene.remove(cloud);cloud.geometry?.dispose();if(Array.isArray(cloud.material))cloud.material.forEach(material=>material.dispose());else cloud.material?.dispose();}
  cloud=points;
  prepareStandardGeometry(cloud.geometry);
  scene.add(cloud);
  const geometry=cloud.geometry;
  normalizeGeometry(geometry);
  const radius=Math.max(geometry.boundingSphere?.radius||1,.001);
  camera.near=Math.max(radius/10000,.0001);camera.far=Math.max(radius*1000,100);camera.position.set(radius*1.5,radius*.9,radius*1.5);camera.updateProjectionMatrix();
  controls.target.set(0,0,0);controls.update();initialView={position:camera.position.clone(),target:controls.target.clone()};
  fileLabel.textContent=name;pointCount.textContent=(geometry.getAttribute('position')?.count||0).toLocaleString()+' 个点';viewerInfo.hidden=false;configureModeControls();
}

function forEachLine(text,callback){
  let start=0;
  while(start<text.length){let end=text.indexOf('\n',start);if(end<0)end=text.length;if(callback(text.slice(start,end).replace(/\r$/,''))===false)break;start=end+1;}
}

async function forEachLineAsync(text,callback,onProgress){
  let start=0,linesSincePaint=0;
  while(start<text.length){
    let end=text.indexOf('\n',start);if(end<0)end=text.length;
    callback(text.slice(start,end).replace(/\r$/,''));
    start=end+1;linesSincePaint+=1;
    if(linesSincePaint>=10000){linesSincePaint=0;onProgress(start/Math.max(text.length,1));await nextPaint();}
  }
  onProgress(1);
}

function normalizeFieldName(name){return name.trim().replace(/^[/#]+/,'').toLowerCase();}

function schemaFromText(text){
  let schema=null,inspected=0;
  forEachLine(text,raw=>{
    if(schema||inspected>=50)return false;
    const line=raw.trim();if(!line)return;inspected+=1;
    const fields=line.replace(/^\/\//,'').replace(/^#/,'').trim().split(/[\s,]+/).map(normalizeFieldName);
    const x=fields.indexOf('x'),y=fields.indexOf('y'),z=fields.indexOf('z');if(x<0||y<0||z<0)return;
    const indexOf=(...names)=>{for(const name of names){const index=fields.indexOf(name);if(index>=0)return index;}return -1;};
    schema={x,y,z,r:indexOf('r','red'),g:indexOf('g','green'),b:indexOf('b','blue'),semantic:indexOf('sem_class','semantic_class','semclass','semantic'),instance:indexOf('inst_class','instance_class','instclass','instance')};return false;
  });
  return schema;
}

function valuesFromLine(raw,extension){
  let line=raw.trim();if(!line||line.startsWith('#')||line.startsWith('//'))return null;
  if(extension==='obj'){if(!line.startsWith('v '))return null;line=line.slice(2).trim();}
  return line.split(/[\s,]+/).map(Number);
}

function makeLegend(counts,mode){
  return [...counts.entries()].sort((a,b)=>{const an=Number(a[0]),bn=Number(b[0]);return Number.isFinite(an)&&Number.isFinite(bn)?an-bn:String(a[0]).localeCompare(String(b[0]));}).map(([value,count])=>({value,count,color:colorForClass(value,mode).getHex()}));
}

async function geometryFromText(text,extension){
  const header=schemaFromText(text);
  const schema=header||{x:0,y:1,z:2,r:3,g:4,b:5,semantic:-1,instance:-1};
  let capacity=0,maxColumns=0;
  await forEachLineAsync(text,raw=>{const values=valuesFromLine(raw,extension);if(values&&values.length>Math.max(schema.x,schema.y,schema.z)){capacity+=1;maxColumns=Math.max(maxColumns,values.length);}},ratio=>setStageProgress('parse',ratio*30));
  if(!capacity)throw new Error('文件中没有可解析的点坐标');

  const positions=new Float32Array(capacity*3);
  const hasRgb=schema.r>=0&&schema.g>=0&&schema.b>=0&&maxColumns>Math.max(schema.r,schema.g,schema.b);
  const hasSemantic=schema.semantic>=0,hasInstance=schema.instance>=0;
  const rgbColors=hasRgb?new Float32Array(capacity*3):null;
  const semanticColors=hasSemantic?new Float32Array(capacity*3):null;
  const instanceColors=hasInstance?new Float32Array(capacity*3):null;
  const semanticCounts=new Map(),instanceCounts=new Map(),color=new THREE.Color();
  let count=0;

  await forEachLineAsync(text,raw=>{
    const values=valuesFromLine(raw,extension);if(!values)return;
    const x=values[schema.x],y=values[schema.y],z=values[schema.z];if(![x,y,z].every(Number.isFinite))return;
    const offset=count*3;positions[offset]=x;positions[offset+1]=y;positions[offset+2]=z;
    if(rgbColors){const r=values[schema.r],g=values[schema.g],b=values[schema.b],scale=Math.max(r,g,b)>1?255:1;rgbColors[offset]=Number.isFinite(r)?Math.min(Math.max(r/scale,0),1):1;rgbColors[offset+1]=Number.isFinite(g)?Math.min(Math.max(g/scale,0),1):1;rgbColors[offset+2]=Number.isFinite(b)?Math.min(Math.max(b/scale,0),1):1;}
    if(semanticColors){const value=values[schema.semantic],key=Number.isFinite(value)?String(value):'未知';colorForClass(key,'semantic',color);semanticColors[offset]=color.r;semanticColors[offset+1]=color.g;semanticColors[offset+2]=color.b;semanticCounts.set(key,(semanticCounts.get(key)||0)+1);}
    if(instanceColors){const value=values[schema.instance],key=Number.isFinite(value)?String(value):'未知';colorForClass(key,'instance',color);instanceColors[offset]=color.r;instanceColors[offset+1]=color.g;instanceColors[offset+2]=color.b;instanceCounts.set(key,(instanceCounts.get(key)||0)+1);}
    count+=1;
  },ratio=>setStageProgress('parse',30+ratio*65));
  if(!count)throw new Error('文件中没有可解析的点坐标');

  const geometry=new THREE.BufferGeometry();
  geometry.setAttribute('position',new THREE.BufferAttribute(positions.subarray(0,count*3),3));geometry.userData.colorModes={};
  if(rgbColors){geometry.setAttribute('rgbColor',new THREE.BufferAttribute(rgbColors.subarray(0,count*3),3));geometry.userData.colorModes.rgb={available:true,legend:[]};}
  if(semanticColors){geometry.setAttribute('semanticColor',new THREE.BufferAttribute(semanticColors.subarray(0,count*3),3));geometry.userData.colorModes.semantic={available:true,legend:makeLegend(semanticCounts,'semantic')};}
  if(instanceColors){geometry.setAttribute('instanceColor',new THREE.BufferAttribute(instanceColors.subarray(0,count*3),3));geometry.userData.colorModes.instance={available:true,legend:makeLegend(instanceCounts,'instance')};}
  const initial=geometry.getAttribute('rgbColor')||geometry.getAttribute('semanticColor')||geometry.getAttribute('instanceColor');if(initial)geometry.setAttribute('color',initial);
  setStageProgress('parse',100);
  return geometry;
}

async function loadPointCloud(url){
  const path=new URL(url,window.location.href).pathname,name=decodeURIComponent(path.split('/').pop()||'point-cloud'),extension=(name.split('.').pop()||'').toLowerCase();
  if(!['ply','pcd','xyz','txt','pts','obj'].includes(extension))throw new Error('暂不支持 .'+extension+' 格式。请返回 PLY、PCD、XYZ、PTS、TXT 或仅含顶点的 OBJ 点云文件。');
  const buffer=await fetchPointCloudData(url);
  await nextPaint();
  let points;
  if(extension==='ply'){
    setStageProgress('parse',5);await nextPaint();
    const geometry=prepareStandardGeometry(new PLYLoader().parse(buffer));
    setStageProgress('parse',90);await nextPaint();
    points=new THREE.Points(geometry,pointMaterial(Boolean(geometry.getAttribute('rgbColor'))));
  }else if(extension==='pcd'){
    setStageProgress('parse',5);await nextPaint();
    points=new PCDLoader().parse(buffer,url);
    prepareStandardGeometry(points.geometry);points.material.size=Number(pointSize.value);points.material.sizeAttenuation=true;
    setStageProgress('parse',90);await nextPaint();
  }else{
    setStageProgress('parse',1);await nextPaint();
    const geometry=await geometryFromText(new TextDecoder().decode(buffer),extension);
    points=new THREE.Points(geometry,pointMaterial(Boolean(geometry.getAttribute('color'))));
  }
  installCloud(points,name);
  setStageProgress('parse',100);
  await nextPaint();
  state.hidden=true;
}

async function openViewer({taskId,resultUrl}){
  pointCloudSection.hidden=false;taskLabel.textContent=taskId;viewerInfo.hidden=true;colorLegend.hidden=true;state.hidden=false;state.className='viewer-state';state.innerHTML=loadingMarkup();pointCloudSection.scrollIntoView({behavior:'smooth',block:'start'});
  if(!animationStarted){animationStarted=true;animate();}
  try{await loadPointCloud(resultUrl);}catch(error){showError(error.message||String(error));}
}

modeButtons.forEach(button=>button.addEventListener('click',()=>setColorMode(button.dataset.colorMode)));
pointSize.addEventListener('input',()=>{if(cloud?.material){cloud.material.size=Number(pointSize.value);cloud.material.needsUpdate=true;}});
resetView.addEventListener('click',()=>{if(!initialView)return;camera.position.copy(initialView.position);controls.target.copy(initialView.target);controls.update();});
window.addEventListener('resize',resize);
closeViewer.addEventListener('click',()=>{pointCloudSection.hidden=true;document.getElementById('downloadTaskId').scrollIntoView({behavior:'smooth',block:'center'});});
window.pointCloudViewer={open:openViewer};
})().catch(error=>{const state=document.getElementById('viewerState');if(state){state.className='viewer-state error';state.innerHTML='<strong>点云查看器加载失败</strong><p></p>';state.querySelector('p').textContent=error.message||String(error);}});
