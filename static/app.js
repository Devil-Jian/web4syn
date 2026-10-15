const $ = id => document.getElementById(id);
const state={role:null,module:'quality',task:null,points:[],started:0,timer:null,urls:[],taskCache:{},prefetch:new Map(),prefetchBusy:0,prefetchQueue:[],prefetchGeneration:0,openSeq:0,prefetchTimer:null,prefetchPaused:false};
const show=(id,on=true)=>$(id).hidden=!on;
const notify=msg=>{const n=$('notice');n.textContent=msg;n.style.display='block';setTimeout(()=>n.style.display='none',4500)};
async function request(path,opts={}){
 const headers={...(opts.headers||{})};
 if(opts.body && !(opts.body instanceof FormData))headers['Content-Type']='application/json';
 const r=await fetch(path,{...opts,headers,cache:'no-store'});
 if(!r.ok){let text=await r.text();try{text=JSON.parse(text).detail||text}catch{}throw Error(String(text))}
 return r;
}
async function json(path,opts={}){return(await request(path,opts)).json()}
function resetImages(){state.urls.forEach(URL.revokeObjectURL);state.urls=[];$('images').replaceChildren()}
function disposePrepared(prepared){
 if(prepared?.imageAssets)for(const asset of prepared.imageAssets){
  asset.img.removeAttribute('src');
  URL.revokeObjectURL(asset.url);
 }
}
function discardEntry(entry){
 entry.discarded=true;
 if(entry.prepared)disposePrepared(entry.prepared);
}
function dropPrefetch(){
 if(state.prefetchTimer!==null){clearTimeout(state.prefetchTimer);state.prefetchTimer=null}
 state.prefetchGeneration++;
 for(const entry of state.prefetch.values())discardEntry(entry);
 state.prefetch.clear();
 state.prefetchQueue=[];
}
function dashboard(){loadSummary();state.openSeq++;show('dashboard');show('tasklist',false);show('reader',false);resetImages();dropPrefetch()}
function goList(){state.openSeq++;show('dashboard',false);show('tasklist');show('reader',false);clearInterval(state.timer);resetImages();dropPrefetch();loadTasks()}
function boot(){dashboard();loadSummary()}
document.querySelectorAll('[data-module]').forEach(b=>b.onclick=()=>{state.module=b.dataset.module;goList()});
$('back').onclick=dashboard;$('back-list').onclick=goList;
let listPage=1,listPages=1,listStatus='pending',listSeq=0;
const stream={module:null,after:0,hasMore:true,busy:false,loading:null};
async function loadSummary(){
 try{
  const data=await json('/api/tasks/summary');
  for(const module of ['quality','diagnosis']){
   const v=data[module],local=outbox.filter(x=>x.module===module).length;
   $(module+'-count').textContent='总任务 '+v.total+' · 已完成 '+v.submitted+' · 待评阅 '+v.remaining+(local?' · 本地待同步 '+local:'');
  }
 }catch(err){console.warn('Task summary failed',err)}
}
function markLocalTasks(items){
 for(const t of items)if(outbox.some(q=>q.id===t.id))t.status='queued';
 return items;
}
async function loadTasks(){
 const seq=++listSeq;
 $('list-title').textContent=state.module==='quality'?'图像质量任务':'诊断效能任务';
 try{
  const data=await json('/api/tasks?module='+state.module+'&status='+listStatus+'&page='+listPage+'&page_size=50');
  if(seq!==listSeq)return;
  listPages=data.pages;
  $('task-total').textContent='共 '+data.total+' 条 · 每页 50 条';
  $('page-info').textContent='第 '+data.page+' / '+data.pages+' 页';
  $('prev-page').disabled=listPage<=1;
  $('next-page').disabled=listPage>=data.pages;
  const list=$('task-rows');list.replaceChildren();
  const items=markLocalTasks(data.items);
  if(!items.length){list.textContent='此筛选条件下暂无任务';return}
  items.forEach(t=>{
   const row=document.createElement('div');row.className='task-row';
   const info=document.createElement('div'),title=document.createElement('strong');
   title.textContent=t.public_code;
   const meta=document.createElement('small');
   meta.textContent=' · '+(t.status==='submitted'?'已提交':t.status==='queued'?'本地待同步':'待评阅');
   info.append(title,meta);row.append(info);
   const button=document.createElement('button');
   button.textContent=t.status==='submitted'?'已完成':t.status==='queued'?'待同步':'开始评阅';
   button.disabled=t.status==='submitted'||t.status==='queued';
   button.onclick=()=>startStream(t.id);
   row.append(button);list.append(row);
  });
 }catch(err){notify(err.message)}
}
$('task-filter').onchange=e=>{listStatus=e.target.value;listPage=1;loadTasks()};
$('prev-page').onclick=()=>{if(listPage>1){listPage--;loadTasks()}};
$('next-page').onclick=()=>{if(listPage<listPages){listPage++;loadTasks()}};
async function appendStreamBatch(){
 if(stream.loading)return stream.loading;
 if(!stream.hasMore||stream.module!==state.module)return;
 stream.busy=true;
 const job=(async()=>{try{
  const batch=await json('/api/tasks/next?module='+stream.module+'&after_id='+stream.after+'&limit=50');
  if(stream.module!==state.module)return;
  const items=markLocalTasks(batch.items);
  if(items.length)stream.after=items[items.length-1].id;
  stream.hasMore=batch.has_more;
  const existing=state.taskCache[stream.module]||[];
  const known=new Set(existing.map(x=>x.id));
  existing.push(...items.filter(t=>!known.has(t.id)));
  // Retain only the current bounded reading window, not the full index.
  const activeId=state.task?.id||0;
  while(existing.length>100&&existing[0].id<activeId&&!state.prefetch.has(existing[0].id))existing.shift();
  state.taskCache[stream.module]=existing;
 }finally{stream.busy=false}})();
 stream.loading=job;
 try{return await job}finally{if(stream.loading===job)stream.loading=null}
}
async function startStream(id=0){
 state.taskCache[state.module]=[];
 stream.module=state.module;stream.after=Math.max(0,id-1);stream.hasMore=true;
 try{
  await appendStreamBatch();
  while(!id&&!pendingTasks().length&&stream.hasMore)await appendStreamBatch();
  const first=id||pendingTasks()[0]?.id;
  if(!first||!pendingTasks().some(t=>t.id===first)){notify('没有可评阅的任务');return}
  await openTask(first);
 }catch(err){notify(err.message)}
}
$('continue-review').onclick=()=>startStream();
function tick(){const s=Math.floor((Date.now()-state.started)/1000);$('elapsed').textContent=String(Math.floor(s/60)).padStart(2,'0')+':'+String(s%60).padStart(2,'0')}
function pendingTasks(){
 return (state.taskCache[state.module]||[]).filter(t=>t.status!=='submitted'&&t.status!=='queued'&&!outbox.some(q=>q.id===t.id));
}
function nextTaskId(current){
 const pending=pendingTasks();
 return pending.find(t=>t.id>current)?.id??pending[0]?.id??null;
}
// While the doctor reviews the current case, keep 5 upcoming quality images
// or 3 upcoming diagnosis cases (each normally has NCCT + AP/PVP/DP) preloaded.
// Once the next case is displayed, refill the sliding window in the background.
// Limit concurrent task loads to 2 to avoid flooding the tunnel / HDF5 reader.
const prefetchLimit=()=>state.module==='quality'?5:3;
async function decodeImageBlob(blob){
 const url=URL.createObjectURL(blob);
 const img=new Image();
 img.alt='匿名二维 CT';
 img.draggable=false;
 img.src=url;
 try{
  await img.decode();
  return {url,img};
 }catch(err){
  img.removeAttribute('src');
  URL.revokeObjectURL(url);
  throw err;
 }
}
async function fetchTaskImages(id,background=false){
 const task=await json('/api/tasks/'+id+(background?'?prefetch=true':''));
 if(!task.images?.length)throw Error('任务没有可展示的影像');
 // Download phase images concurrently, then decode while the doctor is reading.
 const imageBlobs=[];
 for(const item of task.images)imageBlobs.push(await (await request(item.url)).blob());
 const imageAssets=[];
 try{
  // Decode one by one so failures can release all previously allocated URLs.
  for(const blob of imageBlobs)imageAssets.push(await decodeImageBlob(blob));
  return {task,imageAssets};
 }catch(err){
  disposePrepared({imageAssets});
  throw err;
 }
}
function pumpPrefetch(){
 if(state.prefetchPaused||$('reader').hidden)return;
 while(state.prefetchBusy<1&&state.prefetchQueue.length){
  const entry=state.prefetchQueue.shift();
  if(entry.generation!==state.prefetchGeneration||!state.prefetch.has(entry.id))continue;
  state.prefetchBusy++;
  fetchTaskImages(entry.id,true).then(prepared=>{
   if(entry.discarded||entry.generation!==state.prefetchGeneration){
    disposePrepared(prepared);
    entry.reject(new Error('Prefetch cancelled'));
   }else{
    entry.prepared=prepared;
    entry.resolve(prepared);
   }
  },entry.reject).finally(()=>{
   state.prefetchBusy--;
   pumpPrefetch();
  });
 }
}
function preloadTask(id){
 if(!id||state.prefetch.has(id))return;
 let resolve,reject;
 const promise=new Promise((ok,fail)=>{resolve=ok;reject=fail});
 const entry={id,promise,resolve,reject,generation:state.prefetchGeneration,discarded:false,prepared:null};
 state.prefetch.set(id,entry);
 state.prefetchQueue.push(entry);
 promise.catch(()=>{if(state.prefetch.get(id)===entry)state.prefetch.delete(id)});
 pumpPrefetch();
}
function warmNextTasks(currentId){
 const queue=pendingTasks();
 const currentIndex=queue.findIndex(t=>t.id===currentId);
 const ids=queue.slice(currentIndex<0?0:currentIndex+1,(currentIndex<0?0:currentIndex+1)+prefetchLimit()).map(t=>t.id);
 // Only retain the lookahead window, never the displayed task.
 for(const [id,entry] of state.prefetch)if(!ids.includes(id)){
  discardEntry(entry);
  state.prefetch.delete(id);
 }
 state.prefetchQueue=state.prefetchQueue.filter(e=>state.prefetch.has(e.id));
 ids.forEach(preloadTask);
}
async function openTask(id){
 if(state.prefetchTimer!==null){clearTimeout(state.prefetchTimer);state.prefetchTimer=null}
 state.prefetchPaused=true;
 const seq=++state.openSeq;
 try{
  const entry=state.prefetch.get(id);
  let prepared;
  try{prepared=entry?await entry.promise:await fetchTaskImages(id)}
  catch(err){
   // A cancelled/failed prefetch is retried once through the normal loader.
   if(seq!==state.openSeq)return;
   if(entry)state.prefetch.delete(id);
   prepared=await fetchTaskImages(id);
  }
  if(seq!==state.openSeq){if(!entry)disposePrepared(prepared);return}
  if(entry)state.prefetch.delete(id);
  if(entry){const opened=await json('/api/tasks/'+id);if(opened.status==='submitted')throw Error('该任务已提交');prepared.task=opened}
  if(seq!==state.openSeq){if(!entry)disposePrepared(prepared);return}
  resetImages();state.points=[];
  const {task,imageAssets}=prepared;
  state.task=task;
  show('tasklist',false);show('reader');$('case-code').textContent=task.public_code;
  $('task-title').textContent=task.module==='quality'?'图像质量评阅':'肝脏病灶诊断评阅';
  show('quality-form',task.module==='quality');show('diagnosis-form',task.module==='diagnosis');
  $('quality-form').reset();$('diagnosis-form').reset();syncDiagnosisChoices();$('points').textContent='已标记 0 个位置';
  task.images.forEach((image,i)=>{
   const asset=imageAssets[i];state.urls.push(asset.url);
  const panel=document.createElement('div');panel.className='image-panel';const title=document.createElement('div');title.className='image-title';title.textContent=task.module==='quality'?'CECT':image.phase;
  const frame=document.createElement('div');frame.className='image-frame';const img=asset.img;img.alt='匿名二维 CT '+image.phase;frame.append(img);
  let zoom=1;
  const tools=document.createElement('div');tools.className='image-tools';
  const plus=document.createElement('button');plus.textContent='放大 +';plus.onclick=()=>{zoom=Math.min(4,zoom+.2);img.style.transform='scale('+zoom+')'};
  const minus=document.createElement('button');minus.textContent='缩小 −';minus.onclick=()=>{zoom=Math.max(.5,zoom-.2);img.style.transform='scale('+zoom+')'};
  const reset=document.createElement('button');reset.textContent='重置';reset.onclick=()=>{zoom=1;img.style.transform='scale(1)'};
  tools.append(plus,minus,reset);
  if(task.module==='diagnosis')frame.onclick=e=>{
   if(document.querySelector('input[name="lesion_visible"]:checked')?.value==='no')return;
   if(e.target!==img && e.target!==frame)return;
   const box=img.getBoundingClientRect();const x=(e.clientX-box.left)/box.width,y=(e.clientY-box.top)/box.height;
   if(x<0||x>1||y<0||y>1||state.points.length>=20)return;
   state.points.push({image_id:image.id,x,y});
   const dot=document.createElement('span');dot.className='point';const bounds=frame.getBoundingClientRect();
   dot.style.left=(e.clientX-bounds.left)+'px';dot.style.top=(e.clientY-bounds.top)+'px';frame.append(dot);
   $('points').textContent='已标记 '+state.points.length+' 个位置';
  };
  panel.append(title,frame,tools);$('images').append(panel);
  });
  clearInterval(state.timer);state.started=Date.now();tick();state.timer=setInterval(tick,1000);
  // Keep a bounded cursor stream available without blocking image display.
  state.prefetchPaused=false;
  requestAnimationFrame(()=>{if(seq===state.openSeq){
   state.prefetchTimer=setTimeout(()=>{
    state.prefetchTimer=null;
    if(seq!==state.openSeq||$('reader').hidden)return;
    warmNextTasks(id);
    if(pendingTasks().length<=8&&stream.hasMore)
     void appendStreamBatch().then(()=>{if(seq===state.openSeq)warmNextTasks(id)}).catch(console.warn);
   },1200);
  }});
 }catch(err){if(seq!==state.openSeq)return;notify('下一张图像加载失败：'+err.message);if(!state.task)goList()}
}
$('clear-points').onclick=()=>{state.points=[];document.querySelectorAll('.point').forEach(x=>x.remove());$('points').textContent='已标记 0 个位置'};
// Local outbox: a rating is staged durably before the UI advances.
// This is browser-local data and must not be treated as server-confirmed.
const OUTBOX_KEY='web4syn-review-outbox-v1';
let outbox=[];
try{
 const stored=JSON.parse(localStorage.getItem(OUTBOX_KEY)||'[]');
 if(Array.isArray(stored))outbox=stored.filter(x=>Number.isInteger(x.id)&&x.payload&&typeof x.payload==='object');
}catch(e){console.warn('Cannot read local outbox',e)}
let uploadActive=false;
let retryTimer=null;
let retryDelay=1500;
function outboxStatus(){
 let el=$('sync-status');
 if(!el){
  el=document.createElement('span');
  el.id='sync-status';
  el.style.cssText='font-size:13px;color:#f8cc81;margin-left:12px';
  const head=document.querySelector('header');
  if(head)head.append(el);
 }
 if(el)el.textContent=outbox.length?'待同步 '+outbox.length+' 条（保留在本浏览器）':'评分已同步';
}
function persistOutbox(){
 // If storage is blocked or full, do not transition to another image.
 localStorage.setItem(OUTBOX_KEY,JSON.stringify(outbox));
 outboxStatus();
}
function scheduleUpload(delay=0){
 if(retryTimer!==null)clearTimeout(retryTimer);
 retryTimer=setTimeout(()=>{retryTimer=null;void flushOutbox()},delay);
}
async function flushOutbox(){
 if(uploadActive||!outbox.length)return;
 uploadActive=true;
 try{
  while(outbox.length){
   const item=outbox[0];
   try{
    await json('/api/tasks/'+item.id+'/submit',{method:'POST',body:JSON.stringify(item.payload)});
   }catch(err){
    // A response may be lost after successful server commit.
    // Reconcile a 409 only if the server confirms submission.
    if(String(err.message).includes('Already submitted')){
     const task=await json('/api/tasks/'+item.id);
     if(task.status!=='submitted')throw err;
    }else throw err;
   }
   outbox.shift();
   persistOutbox();
   retryDelay=1500;
  }
 }catch(err){
  console.warn('Background rating sync failed',err);
  outboxStatus();
  scheduleUpload(retryDelay);
  retryDelay=Math.min(retryDelay*2,30000);
 }finally{uploadActive=false}
}
window.addEventListener('online',()=>scheduleUpload(0));
document.addEventListener('visibilitychange',()=>{if(!document.hidden)scheduleUpload(0)});
outboxStatus();
if(outbox.length)scheduleUpload(0);
let submitting=false;
async function sendReview(payload){
 if(!state.task||submitting)return;
 submitting=true;
 state.prefetchPaused=true;
 if(state.prefetchTimer!==null){clearTimeout(state.prefetchTimer);state.prefetchTimer=null}
 const taskId=state.task.id;
 const submitButton=document.querySelector('#'+(state.task.module==='quality'?'quality-form':'diagnosis-form')+' button[type="submit"]');
 if(submitButton)submitButton.disabled=true;
 try{
  if(!outbox.some(x=>x.id===taskId)){
   const nextOutbox=[...outbox,{id:taskId,payload,createdAt:Date.now()}];
   // A failed local write must prevent advancing and leave the form intact.
   localStorage.setItem(OUTBOX_KEY,JSON.stringify(nextOutbox));
   outbox=nextOutbox;
  }
  outboxStatus();
  // Mark only the local queue; the server state remains pending until synced.
  const current=(state.taskCache[state.module]||[]).find(t=>t.id===taskId);
  if(current)current.status='queued';
  let next=nextTaskId(taskId);
  scheduleUpload(0);
  while(!next&&stream.hasMore){await appendStreamBatch();next=nextTaskId(taskId)}
  if(next){
   notify('评分已在本机暂存，正在打开下一张');
   await openTask(next);
  }else{
   notify('当前模块已评阅完毕；请确认右上角待同步数量归零');
   goList();
  }
 }catch(err){notify('本地保存或切换失败：'+err.message)}
 finally{
  submitting=false;
  if(submitButton)submitButton.disabled=false;
  if(!$('reader').hidden){state.prefetchPaused=false;pumpPrefetch()}
 }
}
$('quality-form').addEventListener('submit',e=>{e.preventDefault();const f=new FormData(e.target);sendReview({guessed_source:f.get('guessed_source'),quality_score:Number(f.get('quality_score'))})});
function syncDiagnosisChoices(){
 const visibility=document.querySelector('input[name="lesion_visible"]:checked')?.value;
 const noLesion=visibility==='no',uncertain=visibility==='unsure';
 for(const box of document.querySelectorAll('input[name="diagnoses"]')){
  if(noLesion)box.checked=false;
  box.disabled=noLesion;
 }
 const confidenceOptions=[...document.querySelectorAll('input[name="confidence"]')];
 for(const box of confidenceOptions){
  if(uncertain)box.checked=false;
  box.disabled=uncertain;
  box.required=visibility==='yes'&&box.value==='1';
 }
 $('diagnosis-hint').textContent=noLesion?'不存在病灶：疾病标签不可选。':visibility==='yes'?'存在病灶：至少选择一种疾病标签。':'不确定：可选择疑似疾病标签。';
 const confidenceHint=$('confidence-hint');
 if(confidenceHint)confidenceHint.textContent=uncertain?'不确定病灶是否存在：无需填写置信度。':visibility==='yes'?'存在病灶：诊断置信度必选。':'不存在病灶：置信度可选。';
 if(noLesion){
  state.points=[];
  document.querySelectorAll('.point').forEach(x=>x.remove());
  $('points').textContent='已标记 0 个位置';
 }
 $('clear-points').disabled=noLesion;
}
document.querySelectorAll('input[name="lesion_visible"]').forEach(x=>x.addEventListener('change',syncDiagnosisChoices));
$('diagnosis-form').addEventListener('submit',e=>{
 e.preventDefault();
 const f=new FormData(e.target);
 const visibility=f.get('lesion_visible'),diagnoses=f.getAll('diagnoses');
 const confidence=f.get('confidence');
 if(visibility==='yes'&&!diagnoses.length){notify('存在病灶时必须选择至少一种疾病标签');return}
 if(visibility==='yes'&&!confidence){notify('存在病灶时必须选择诊断置信度');return}
 if(visibility==='no'&&diagnoses.length){notify('不存在病灶时不能选择疾病标签');return}
 sendReview({lesion_visible:visibility,diagnoses,confidence:confidence?Number(confidence):null,lesion_points:visibility==='no'?[]:[...state.points]});
});
boot();
