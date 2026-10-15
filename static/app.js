const $ = id => document.getElementById(id);
const state={role:null,module:'quality',task:null,points:[],started:0,timer:null,urls:[],taskCache:{},prefetch:new Map(),prefetchBusy:0,prefetchQueue:[],prefetchGeneration:0,openSeq:0};
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
 state.prefetchGeneration++;
 for(const entry of state.prefetch.values())discardEntry(entry);
 state.prefetch.clear();
 state.prefetchQueue=[];
}
function dashboard(){state.openSeq++;show('dashboard');show('tasklist',false);show('reader',false);resetImages();dropPrefetch()}
function goList(){state.openSeq++;show('dashboard',false);show('tasklist');show('reader',false);clearInterval(state.timer);resetImages();dropPrefetch();loadTasks()}
function boot(){dashboard()}
document.querySelectorAll('[data-module]').forEach(b=>b.onclick=()=>{state.module=b.dataset.module;goList()});
$('back').onclick=dashboard;$('back-list').onclick=goList;
async function loadTasks(){
 $('list-title').textContent=state.module==='quality'?'图像质量任务':'诊断效能任务';
 try{const all=(await json('/api/tasks')).filter(t=>t.module===state.module);state.taskCache[state.module]=all;const list=$('task-rows');list.replaceChildren();
 if(!all.length){list.textContent='暂无任务，请联系研究管理员分配。';return}
 all.forEach(t=>{const row=document.createElement('div');row.className='task-row';const info=document.createElement('div');const title=document.createElement('strong');title.textContent=t.public_code;const meta=document.createElement('small');meta.textContent=' · '+(t.status==='submitted'?'已提交':'待评阅');info.append(title,meta);row.append(info);const button=document.createElement('button');button.textContent=t.status==='submitted'?'已完成':'开始评阅';button.disabled=t.status==='submitted';button.onclick=()=>openTask(t.id);row.append(button);list.append(row)})}
 catch(err){notify(err.message)}
}
function tick(){const s=Math.floor((Date.now()-state.started)/1000);$('elapsed').textContent=String(Math.floor(s/60)).padStart(2,'0')+':'+String(s%60).padStart(2,'0')}
function pendingTasks(){
 return (state.taskCache[state.module]||[]).filter(t=>t.status!=='submitted');
}
function nextTaskId(current){
 const pending=pendingTasks();
 const index=pending.findIndex(t=>t.id===current);
 return index>=0?(pending[index+1]?.id??null):(pending[0]?.id??null);
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
async function fetchTaskImages(id){
 const task=await json('/api/tasks/'+id);
 if(!task.images?.length)throw Error('任务没有可展示的影像');
 // Download phase images concurrently, then decode while the doctor is reading.
 const imageBlobs=await Promise.all(task.images.map(async item=>(await request(item.url)).blob()));
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
 while(state.prefetchBusy<2&&state.prefetchQueue.length){
  const entry=state.prefetchQueue.shift();
  if(entry.generation!==state.prefetchGeneration||!state.prefetch.has(entry.id))continue;
  state.prefetchBusy++;
  fetchTaskImages(entry.id).then(prepared=>{
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
  resetImages();state.points=[];
  const {task,imageAssets}=prepared;
  state.task=task;
  show('tasklist',false);show('reader');$('case-code').textContent=task.public_code;
  $('task-title').textContent=task.module==='quality'?'图像质量评阅':'肝脏病灶诊断评阅';
  show('quality-form',task.module==='quality');show('diagnosis-form',task.module==='diagnosis');
  $('quality-form').reset();$('diagnosis-form').reset();$('points').textContent='已标记 0 个位置';
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
  // Refill the sliding lookahead after the new case is painted.
  requestAnimationFrame(()=>{if(seq===state.openSeq)warmNextTasks(id)});
 }catch(err){if(seq!==state.openSeq)return;notify('下一张图像加载失败：'+err.message);if(!state.task)goList()}
}
$('clear-points').onclick=()=>{state.points=[];document.querySelectorAll('.point').forEach(x=>x.remove());$('points').textContent='已标记 0 个位置'};
let submitting=false;
async function sendReview(payload){
 if(!state.task||submitting)return;
 submitting=true;
 const taskId=state.task.id;
 const submitButton=document.querySelector('#'+(state.task.module==='quality'?'quality-form':'diagnosis-form')+' button[type="submit"]');
 if(submitButton)submitButton.disabled=true;
 try{
  await json('/api/tasks/'+taskId+'/submit',{method:'POST',body:JSON.stringify(payload)});
  const current=(state.taskCache[state.module]||[]).find(t=>t.id===taskId);
  if(current)current.status='submitted';
  const next=nextTaskId(taskId);
  if(next){
   notify('已保存，正在进入下一张');
   // Keep the current image visible until the decoded next study is ready.
   await openTask(next);
  }else{
   notify('本模块所有评阅已完成');
   goList();
  }
 }catch(err){notify(err.message)}
 finally{
  submitting=false;
  if(submitButton)submitButton.disabled=false;
 }
}
$('quality-form').addEventListener('submit',e=>{e.preventDefault();const f=new FormData(e.target);sendReview({guessed_source:f.get('guessed_source'),quality_score:Number(f.get('quality_score'))})});
$('diagnosis-form').addEventListener('submit',e=>{e.preventDefault();const f=new FormData(e.target);sendReview({lesion_visible:f.get('lesion_visible'),diagnosis:f.get('diagnosis'),malignancy_score:Number(f.get('malignancy_score')),confidence:Number(f.get('confidence')),lesion_points:state.points})});
boot();
