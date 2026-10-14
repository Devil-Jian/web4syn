const $ = id => document.getElementById(id);
const state={token:sessionStorage.getItem('token'),role:null,module:'quality',task:null,points:[],started:0,timer:null,urls:[]};
const show=(id,on=true)=>$(id).hidden=!on;
const notify=msg=>{const n=$('notice');n.textContent=msg;n.style.display='block';setTimeout(()=>n.style.display='none',4500)};
async function request(path,opts={}){
 const headers={...(opts.headers||{})};
 if(state.token)headers.Authorization='Bearer '+state.token;
 if(opts.body && !(opts.body instanceof FormData))headers['Content-Type']='application/json';
 const r=await fetch(path,{...opts,headers,cache:'no-store'});
 if(!r.ok){let text=await r.text();try{text=JSON.parse(text).detail||text}catch{}throw Error(String(text))}
 return r;
}
async function json(path,opts={}){return(await request(path,opts)).json()}
function resetImages(){state.urls.forEach(URL.revokeObjectURL);state.urls=[];$('images').replaceChildren()}
function dashboard(){show('dashboard');show('tasklist',false);show('reader',false);resetImages()}
function goList(){show('dashboard',false);show('tasklist');show('reader',false);clearInterval(state.timer);resetImages();loadTasks()}
async function boot(){
 if(!state.token){try{const demo=await json('/api/demo/initialize',{method:'POST'});state.token=demo.token;sessionStorage.setItem('token',demo.token);state.demo=true;notify('预览数据：扫描 '+demo.import.files+' 例 HDF5');}catch{show('login');show('workspace',false);return}}
 try{const me=await json('/api/me');state.role=me.role;$('who').textContent=me.display_name+' · '+(me.role==='admin'?'管理员':'医生');show('login',false);show('workspace');show('admin',me.role==='admin'&&!state.demo);$('logout').hidden=!!state.demo;dashboard()}
 catch{sessionStorage.removeItem('token');state.token=null;show('login');show('workspace',false)}
}
$('login-form').addEventListener('submit',async e=>{e.preventDefault();try{const u=await json('/api/auth/login',{method:'POST',body:JSON.stringify({username:$('username').value,password:$('password').value})});state.token=u.token;sessionStorage.setItem('token',u.token);$('password').value='';boot()}catch(err){notify(err.message)}});
$('logout').onclick=()=>{state.token=null;sessionStorage.removeItem('token');clearInterval(state.timer);resetImages();boot()};
document.querySelectorAll('[data-module]').forEach(b=>b.onclick=()=>{state.module=b.dataset.module;goList()});
$('back').onclick=dashboard;$('back-list').onclick=goList;
async function loadTasks(){
 $('list-title').textContent=state.module==='quality'?'图像质量任务':'诊断效能任务';
 try{const all=(await json('/api/tasks')).filter(t=>t.module===state.module);const list=$('task-rows');list.replaceChildren();
 if(!all.length){list.textContent='暂无任务，请联系研究管理员分配。';return}
 all.forEach(t=>{const row=document.createElement('div');row.className='task-row';const info=document.createElement('div');const title=document.createElement('strong');title.textContent=t.public_code;const meta=document.createElement('small');meta.textContent=' · '+(t.status==='submitted'?'已提交':'待评阅');info.append(title,meta);row.append(info);const button=document.createElement('button');button.textContent=t.status==='submitted'?'已完成':'开始评阅';button.disabled=t.status==='submitted';button.onclick=()=>openTask(t.id);row.append(button);list.append(row)})}
 catch(err){notify(err.message)}
}
function tick(){const s=Math.floor((Date.now()-state.started)/1000);$('elapsed').textContent=String(Math.floor(s/60)).padStart(2,'0')+':'+String(s%60).padStart(2,'0')}
async function openTask(id){
 try{
 resetImages();state.points=[];
 const task=await json('/api/tasks/'+id);state.task=task;
 show('tasklist',false);show('reader');$('case-code').textContent=task.public_code;
 $('task-title').textContent=task.module==='quality'?'图像质量评阅':'肝脏病灶诊断评阅';
 show('quality-form',task.module==='quality');show('diagnosis-form',task.module==='diagnosis');
 $('quality-form').reset();$('diagnosis-form').reset();$('points').textContent='已标记 0 个位置';
 for(const image of task.images){
  const res=await request(image.url);const blob=await res.blob();const blobUrl=URL.createObjectURL(blob);state.urls.push(blobUrl);
  const panel=document.createElement('div');panel.className='image-panel';const title=document.createElement('div');title.className='image-title';title.textContent=task.module==='quality'?'CECT':image.phase;
  const frame=document.createElement('div');frame.className='image-frame';const img=document.createElement('img');img.src=blobUrl;img.alt='匿名二维 CT '+image.phase;img.draggable=false;frame.append(img);
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
 }
 clearInterval(state.timer);state.started=Date.now();tick();state.timer=setInterval(tick,1000);
 }catch(err){notify(err.message);goList()}
}
$('clear-points').onclick=()=>{state.points=[];document.querySelectorAll('.point').forEach(x=>x.remove());$('points').textContent='已标记 0 个位置'};
async function sendReview(payload){
 if(!state.task)return;
 if(!confirm('确认提交？提交后本次评阅无法修改。'))return;
 try{await json('/api/tasks/'+state.task.id+'/submit',{method:'POST',body:JSON.stringify(payload)});notify('评阅已提交');goList()}
 catch(err){notify(err.message)}
}
$('quality-form').addEventListener('submit',e=>{e.preventDefault();const f=new FormData(e.target);sendReview({guessed_source:f.get('guessed_source'),quality_score:Number(f.get('quality_score')),guess_confidence:Number(f.get('guess_confidence'))})});
$('diagnosis-form').addEventListener('submit',e=>{e.preventDefault();const f=new FormData(e.target);sendReview({lesion_visible:f.get('lesion_visible'),diagnosis:f.get('diagnosis'),malignancy_score:Number(f.get('malignancy_score')),confidence:Number(f.get('confidence')),further_exam:f.get('further_exam')==='yes',exam_type:f.get('exam_type')||null,lesion_points:state.points})});
$('reader-form').addEventListener('submit',async e=>{e.preventDefault();const data=Object.fromEntries(new FormData(e.target));try{await json('/api/admin/users',{method:'POST',body:JSON.stringify(data)});notify('医生已创建');e.target.reset()}catch(err){notify(err.message)}});
$('upload-form').addEventListener('submit',async e=>{e.preventDefault();const b=e.target.querySelector('button');b.disabled=true;try{await request('/api/admin/images',{method:'POST',body:new FormData(e.target)});notify('影像已上传');e.target.reset()}catch(err){notify(err.message)}finally{b.disabled=false}});
$('import-packs').onclick=async()=>{if(!confirm('扫描 /packs 中的 HDF5，建立预选 2D 病例？不会修改源文件。'))return;const b=$('import-packs');b.disabled=true;try{const r=await json('/api/admin/import-packs',{method:'POST'});notify('扫描 '+r.files+' 文件，新增 '+r.created+' 张记录，错误 '+r.errors.length);$('progress-table').textContent=JSON.stringify(r,null,2)}catch(err){notify(err.message)}finally{b.disabled=false}};
$('generate').onclick=async()=>{try{const r=await json('/api/admin/assignments/generate',{method:'POST'});notify('创建 '+r.created+' 个任务');await refreshProgress()}catch(err){notify(err.message)}};
async function refreshProgress(){try{const r=await json('/api/admin/progress');$('progress-table').textContent=r.map(x=>x.username+' / '+x.module+' / '+x.status+': '+x.count).join('\n')||'暂无任务'}catch(err){notify(err.message)}}
$('progress').onclick=refreshProgress;
document.querySelectorAll('[data-export]').forEach(b=>b.onclick=async()=>{try{const r=await request('/api/admin/export/'+b.dataset.export);const url=URL.createObjectURL(await r.blob());const a=document.createElement('a');a.href=url;a.download=b.dataset.export+'_reviews.csv';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)}catch(err){notify(err.message)}});
boot();
