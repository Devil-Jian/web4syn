"use strict";
const el = id => document.getElementById(id);
const study = { token: sessionStorage.getItem("rct-token"), user: null, task: null,
                z: 0, lo: -160, hi: 240, images: new Map(), generation: 0,
                startTime: 0, busy: false, expanded: null };

function visible(id, show) { el(id).hidden = !show; }
function message(text, bad=false) {
  const node=el("notification"); node.textContent=text; node.className=bad?"error":"ok";
  clearTimeout(message.timer); message.timer=setTimeout(()=>node.textContent="",5500);
}
async function api(path, {method="GET", body=null, binary=false}={}) {
  const headers={};
  if(study.token) headers.Authorization="Bearer "+study.token;
  if(body!==null) headers["Content-Type"]="application/json";
  const response=await fetch("/api/rct"+path,{method,headers,
    body:body===null?undefined:JSON.stringify(body),cache:"no-store"});
  if(!response.ok) {
    let detail="HTTP "+response.status;
    try { const data=await response.json(); detail=data.detail||detail; } catch {}
    if(response.status===401 && study.user) { study.token=null; sessionStorage.removeItem("rct-token"); showLogin(); }
    throw Error(typeof detail==="string"?detail:JSON.stringify(detail));
  }
  return binary?response.blob():response.json();
}
function formData(form) { return Object.fromEntries(new FormData(form)); }
function showLogin() {
  study.user=null;
  visible("login-section",true); visible("admin-section",false);visible("reader-section",false);
  visible("logout",false);el("identity").textContent="未登录";
}
async function enter() {
  const who=await api("/me"); study.user=who;
  visible("login-section",false); visible("logout",true);
  el("identity").textContent=who.display_name+" · "+(who.role==="admin"?"管理员":"评阅医生");
  visible("admin-section",who.role==="admin");visible("reader-section",who.role==="reader");
  if(who.role==="admin") await refreshAdmin(); else await refreshReader();
}
el("login-form").onsubmit=async event=>{
  event.preventDefault();
  const form=event.currentTarget;
  try {
    const result=await api("/auth/login",{method:"POST",body:formData(form)});
    study.token=result.token;sessionStorage.setItem("rct-token",result.token);
    form.reset();await enter();
  } catch(err) { message(err.message,true); }
};
el("logout").onclick=async()=>{
  try {if(study.token)await api("/auth/logout",{method:"POST"});} catch {}
  study.token=null;sessionStorage.removeItem("rct-token");discardImages();showLogin();
};

function smallTable(rows, columns) {
  const table=document.createElement("table");table.className="simple-table";
  const thead=document.createElement("thead");
  const tr=document.createElement("tr"); columns.forEach(([key,label])=>{
    const th=document.createElement("th");th.textContent=label;tr.append(th);
  });thead.append(tr);table.append(thead);
  const body=document.createElement("tbody");
  rows.forEach(row=>{const tr=document.createElement("tr");columns.forEach(([key])=>{
    const td=document.createElement("td");td.textContent=String(row[key]??"—");tr.append(td);
  });body.append(tr);});table.append(body);return table;
}
async function refreshAdmin() {
  const [info,doctors,progress]=await Promise.all([
    api("/admin/study"),api("/admin/readers"),api("/admin/progress")
  ]);
  el("study-status").textContent="状态："+info.status+" · 随机摘要："+(info.randomization_digest||"未随机");
  el("study-form").elements.target.value=info.target;
  const cards=[["病例数",info.patients+"/"+info.target],["医生",info.readers],
    ["任务",info.tasks],["研究状态",info.status]];
  el("admin-metrics").replaceChildren(...cards.map(([name,value])=>{
    const div=document.createElement("div");div.className="metric";
    const num=document.createElement("strong");num.textContent=value;
    const caption=document.createElement("span");caption.textContent=name;
    div.append(num,caption);return div;
  }));
  el("reader-list").replaceChildren(smallTable(doctors,[["username","医生"],["level","资历"],
    ["assigned","任务"],["completed","完成"]]));
  const counts=new Map();
  for(const entry of progress.by_arm) {
    const row=counts.get(entry.arm)||{arm:entry.arm,assigned:0,opened:0,submitted:0};
    row[entry.status]=entry.total;counts.set(entry.arm,row);
  }
  el("progress-table").replaceChildren(smallTable([...counts.values()],
    [["arm","分组（仅管理员）"],["assigned","未开始"],["opened","已打开"],["submitted","已完成"]]));
  const draft=info.status==="draft";
  el("study-form").querySelector("button").disabled=!draft||info.tasks>0;
  el("reader-form").querySelector("button").disabled=!draft||info.tasks>0;
  el("import-form").querySelector("button").disabled=!draft||info.tasks>0;
  el("random-form").querySelector("button").disabled=!draft||info.tasks>0;
  el("publish-button").disabled=!draft||info.tasks!==info.target;
  el("lock-button").disabled=info.status!=="published";
  el("export-button").disabled=info.status!=="locked";
}
el("admin-refresh").onclick=()=>refreshAdmin().catch(err=>message(err.message,true));
el("progress-refresh").onclick=()=>refreshAdmin().catch(err=>message(err.message,true));
el("study-form").onsubmit=async e=>{
  e.preventDefault();
  try{await api("/admin/study",{method:"PUT",body:{target:Number(e.currentTarget.elements.target.value)}});
      message("已保存目标病例数");await refreshAdmin();}catch(err){message(err.message,true);}
};
el("reader-form").onsubmit=async e=>{
  e.preventDefault();
  const form=e.currentTarget;
  try{await api("/admin/readers",{method:"POST",body:formData(form)});
    form.reset();message("医生账户已创建");await refreshAdmin();}
  catch(err){message(err.message,true);}
};
el("packs-button").onclick=async()=>{
  try{
    const response=await api("/admin/packs");visible("pack-list",true);
    el("pack-list").textContent=response.packs.join("\n")||"未挂载HDF5文件";
  }catch(err){message(err.message,true);}
};
el("import-form").onsubmit=async e=>{
  e.preventDefault();
  const form=e.currentTarget;
  const lines=form.elements.rows.value.split(/\r?\n/).map(x=>x.trim()).filter(Boolean);
  const patients=[];
  for(const line of lines) {
    if(line.toLowerCase()==="patient_code,pack_filename,center")continue;
    const cols=line.split(",").map(x=>x.trim());
    if(cols.length!==3||cols.some(x=>!x)) {message("CSV格式错误："+line,true);return;}
    patients.push({patient_code:cols[0],pack_filename:cols[1],center:cols[2]});
  }
  if(!patients.length) {message("请填写至少1名患者",true);return;}
  if(!confirm("导入 "+patients.length+" 名患者？请确认数据已匿名化。"))return;
  try{
    const data=await api("/admin/patients/import",{method:"POST",body:{patients}});
    message("成功导入 "+data.imported+" 名患者");form.reset();await refreshAdmin();
  }catch(err){message(err.message,true);}
};
el("random-form").onsubmit=async e=>{
  e.preventDefault();
  if(!confirm("随机化只能执行一次。确定已经检查病例数、中心与医生名单？"))return;
  const seed=e.currentTarget.elements.seed.value.trim();
  try{
    const res=await api("/admin/randomize",{method:"POST",body:{seed:seed||null}});
    el("rand-result").textContent="已随机化："+JSON.stringify(res.arms)+" · 种子SHA256："+res.seed_sha256;
    await refreshAdmin(); message("随机化已锁定，无法重新抽取");
  }catch(err){message(err.message,true);}
};
el("publish-button").onclick=async()=>{
  if(!confirm("发布后医生将可以阅片。确定发布全部任务？"))return;
  try{await api("/admin/publish",{method:"POST"});await refreshAdmin();message("任务已发布");}
  catch(err){message(err.message,true);}
};
el("lock-button").onclick=async()=>{
  if(!confirm("只有完成全部任务才可锁定；锁定后不能再阅片。确定锁定？"))return;
  try{await api("/admin/lock",{method:"POST"});await refreshAdmin();message("数据库已锁定");}
  catch(err){message(err.message,true);}
};
el("export-button").onclick=async()=>{
  try{
    const blob=await api("/admin/export.csv",{binary:true});
    const url=URL.createObjectURL(blob);
    const a=document.createElement("a");a.href=url;a.download="rct_results.csv";a.click();
    setTimeout(()=>URL.revokeObjectURL(url),1000);
  }catch(err){message(err.message,true);}
};

async function refreshReader(){
  const data=await api("/tasks/summary");
  el("reader-summary").textContent="已完成 "+data.completed+"/"+data.total+" · 剩余 "+data.remaining;
  if(data.remaining===0) {visible("reader-idle",true);el("reader-idle").textContent="已完成全部评阅任务。";}
}
function discardImages() {
  study.generation++;for(const url of study.images.values())URL.revokeObjectURL(url);
  study.images.clear();study.expanded=null;
}
async function startNext(){
  if(study.busy)return;
  try {
    const response=await api("/tasks/next");
    if(!response.task){study.task=null;visible("reading",false);visible("reader-idle",true);
      el("reader-idle").textContent="当前没有待评阅任务。";await refreshReader();return;}
    discardImages();
    const task=await api("/tasks/"+response.task.id);
    study.task=task;study.z=Math.floor(task.depth/2);study.lo=-160;study.hi=240;
    el("window-lo").value=-160;el("window-hi").value=240;
    el("case-number").textContent=task.public_code;
    el("reading-status").textContent="单次独立诊断 · 阅片中";
    el("slice-range").max=Math.max(0,task.depth-1);
    el("slice-range").value=study.z;
    el("diagnosis-form").reset();syncDiagnosisFields();
    visible("reading",true);visible("reader-idle",false);
    const grid=el("phase-grid");grid.replaceChildren();
    task.phases.forEach(phase=>{
      const pane=document.createElement("div");pane.className="phase";
      pane.dataset.phase=phase;
      const heading=document.createElement("span");heading.textContent=phase;
      const img=document.createElement("img");img.alt=phase+" CT";
      pane.append(heading,img);
      pane.onclick=()=>{study.expanded=study.expanded===phase?null:phase;
        document.querySelectorAll(".phase").forEach(node=>node.classList.toggle("expanded",node.dataset.phase===study.expanded));
        grid.classList.toggle("focus",!!study.expanded);
      };
      grid.append(pane);
    });
    study.startTime=Date.now();
    await renderImages();
  }catch(err){message(err.message,true);}
}
el("continue-button").onclick=startNext;
function imageKey(phase,z){return [study.task.id,phase,z,study.lo,study.hi].join("|");}
async function renderImages(){
  if(!study.task)return;
  const seq=++study.generation;
  const task=study.task, z=study.z;
  el("slice-position").textContent=(z+1)+" / "+task.depth;
  el("slice-range").value=z;
  await Promise.all(task.phases.map(async phase=>{
    const pane=[...document.querySelectorAll(".phase")].find(node=>node.dataset.phase===phase);
    const img=pane?.querySelector("img");if(!img)return;
    const key=imageKey(phase,z);let url=study.images.get(key);
    try{
      if(!url){
        const blob=await api("/tasks/"+task.id+"/images/"+phase+"/"+z+
          "?lo="+encodeURIComponent(study.lo)+"&hi="+encodeURIComponent(study.hi),{binary:true});
        url=URL.createObjectURL(blob);study.images.set(key,url);
      }
      if(seq===study.generation&&study.task?.id===task.id)img.src=url;
    }catch(err){
      if(seq===study.generation){img.removeAttribute("src");message("切片读取失败："+err.message,true);}
    }
  }));
  // Preserve current images while releasing older blob URLs.
  if(study.images.size>32){
    const keep=new Set(task.phases.map(p=>imageKey(p,z)));
    for(const [key,url] of study.images){
      if(study.images.size<=20)break;
      if(!keep.has(key)){URL.revokeObjectURL(url);study.images.delete(key);}
    }
  }
}
function moveSlice(delta){
  if(!study.task)return;
  study.z=Math.min(study.task.depth-1,Math.max(0,study.z+delta));renderImages();
}
el("prev-slice").onclick=()=>moveSlice(-1);
el("next-slice").onclick=()=>moveSlice(1);
el("slice-range").oninput=e=>{study.z=Number(e.target.value);renderImages();};
el("phase-grid").addEventListener("wheel",e=>{if(!study.task)return;e.preventDefault();moveSlice(e.deltaY>0?1:-1);},{passive:false});
document.addEventListener("keydown",e=>{
  if(!study.task||el("reading").hidden||["INPUT","TEXTAREA","SELECT"].includes(document.activeElement?.tagName))return;
  if(e.key==="ArrowUp"||e.key==="ArrowDown"){e.preventDefault();moveSlice(e.key==="ArrowDown"?1:-1);}
});
el("window-apply").onclick=()=>{
  const lo=Number(el("window-lo").value),hi=Number(el("window-hi").value);
  if(!Number.isFinite(lo)||!Number.isFinite(hi)||lo< -1200||hi>3000||hi-lo<1){
    message("窗位范围无效",true);return;
  }
  study.lo=lo;study.hi=hi;renderImages();
};
el("window-reset").onclick=()=>{
  el("window-lo").value=-160;el("window-hi").value=240;
  study.lo=-160;study.hi=240;renderImages();
};
function syncDiagnosisFields(){
  const form=el("diagnosis-form");
  const present=form.querySelector('input[name="lesion_visible"]:checked')?.value;
  const diag=form.querySelectorAll('input[name="diagnosis"]');
  const risks=form.querySelectorAll('input[name="malignancy_risk"]');
  diag.forEach(r=>{r.disabled=present==="no";if(r.disabled)r.checked=false;});
  risks.forEach(r=>{r.disabled=present==="unsure";if(r.disabled)r.checked=false;});
  el("diagnosis-field").classList.toggle("disabled",present==="no");
  el("risk-field").classList.toggle("disabled",present==="unsure");
}
el("diagnosis-form").querySelectorAll('input[name="lesion_visible"]').forEach(r=>r.onchange=syncDiagnosisFields);
el("diagnosis-form").onsubmit=async e=>{
  e.preventDefault();
  if(!study.task||study.busy)return;
  const values=formData(e.currentTarget);
  if(values.lesion_visible==="yes"&&(!values.diagnosis||!values.malignancy_risk)){
    message("存在病灶时，必须选择疾病和恶性风险",true);return;
  }
  study.busy=true;el("submit-button").disabled=true;
  const payload={
    submission_key: crypto.randomUUID()+"-"+Date.now(),
    lesion_visible:values.lesion_visible,
    diagnosis:values.diagnosis||null,
    malignancy_risk:values.malignancy_risk?Number(values.malignancy_risk):null,
    client_active_seconds:Math.max(0,(Date.now()-study.startTime)/1000)
  };
  const id=study.task.id;
  // Preserve the exact key and answer for idempotent retry on network failure.
  const key="rct-pending-"+id;
  try{
    const pending=JSON.parse(sessionStorage.getItem(key)||"null");
    const submitted=pending||payload;
    sessionStorage.setItem(key,JSON.stringify(submitted));
    await api("/tasks/"+id+"/submit",{method:"POST",body:submitted});
    sessionStorage.removeItem(key);
    study.task=null;discardImages();visible("reading",false);
    study.busy=false;
    await refreshReader();await startNext();
  }catch(err){message("提交未确认，当前任务保留。请检查网络后再次点击提交："+err.message,true);}
  finally{study.busy=false;el("submit-button").disabled=false;}
};
(async()=>{if(study.token){try{await enter();return;}catch{study.token=null;sessionStorage.removeItem("rct-token");}}showLogin();})();
