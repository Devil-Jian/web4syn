"use strict";
const el = id => document.getElementById(id);
const study = { token: sessionStorage.getItem("rct-token"), user: null, task: null,
                z: 0, lo: -160, hi: 240, images: new Map(), generation: 0, transforms: new Map(),
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
const readersUI={current:null,mode:null};
function closeReaderEditors(){
  visible("reader-edit-panel",false);
  visible("reader-password-panel",false);
  el("reader-password-form").reset();
  readersUI.current=null;readersUI.mode=null;
}
function renderReaderManager(doctors,info){
  const table=document.createElement("table");table.className="simple-table reader-table";
  const head=document.createElement("thead");const tr=document.createElement("tr");
  ["账号","姓名/代号","资历","状态","任务","完成","账户操作"].forEach(label=>{
    const th=document.createElement("th");th.textContent=label;tr.append(th);
  });head.append(tr);table.append(head);
  const body=document.createElement("tbody");
  for(const doctor of doctors){
    const row=document.createElement("tr");
    for(const value of [doctor.username,doctor.display_name,
      ({junior:"初级",intermediate:"中级",senior:"高级"})[doctor.level]||doctor.level,
      doctor.enabled?"启用":"停用",doctor.assigned,doctor.completed]){
      const td=document.createElement("td");td.textContent=String(value??"—");row.append(td);
    }
    const actions=document.createElement("td");actions.className="reader-actions";
    const edit=document.createElement("button");edit.className="quiet";
    edit.type="button";edit.textContent="编辑";
    edit.onclick=()=>{
      closeReaderEditors();readersUI.current=doctor;readersUI.mode="edit";
      const form=el("reader-edit-form");
      form.elements.display_name.value=doctor.display_name;
      form.elements.level.value=doctor.level;
      // The grade is study protocol metadata and is fixed after assignments exist.
      form.elements.level.disabled=info.status!=="draft"||info.tasks>0;
      el("reader-edit-title").textContent="编辑医生 · "+doctor.username;
      visible("reader-edit-panel",true);
    };
    actions.append(edit);
    const pw=document.createElement("button");pw.type="button";pw.className="quiet";
    pw.textContent="重置密码";pw.onclick=()=>{
      closeReaderEditors();readersUI.current=doctor;readersUI.mode="password";
      el("reader-password-title").textContent="重置密码 · "+doctor.username;
      visible("reader-password-panel",true);
    };
    actions.append(pw);
    const toggle=document.createElement("button");toggle.type="button";
    toggle.className=doctor.enabled?"danger-outline":"quiet";
    toggle.textContent=doctor.enabled?"停用":"启用";
    toggle.disabled=info.status!=="draft"||doctor.assigned>0;
    toggle.onclick=async()=>{
      if(!confirm((doctor.enabled?"停用":"启用")+"账户 "+doctor.username+"？"))return;
      try{
        await api("/admin/readers/"+doctor.id+"/status",{
          method:"PATCH",body:{enabled:!doctor.enabled}
        });closeReaderEditors();await refreshAdmin();message("医生账户状态已更新");
      }catch(err){message(err.message,true);}
    };
    actions.append(toggle);row.append(actions);body.append(row);
  }
  table.append(body);el("reader-list").replaceChildren(table);
}
el("reader-edit-cancel").onclick=closeReaderEditors;
el("reader-password-cancel").onclick=closeReaderEditors;
el("reader-edit-form").onsubmit=async e=>{
  e.preventDefault();const doctor=readersUI.current;
  if(!doctor||readersUI.mode!=="edit")return;
  const form=e.currentTarget;
  try{
    await api("/admin/readers/"+doctor.id,{
      method:"PATCH",body:{display_name:form.elements.display_name.value.trim(),
        level:form.elements.level.disabled?doctor.level:form.elements.level.value}
    });
    closeReaderEditors();await refreshAdmin();message("医生信息已保存");
  }catch(err){message(err.message,true);}
};
el("reader-password-form").onsubmit=async e=>{
  e.preventDefault();const doctor=readersUI.current;
  if(!doctor||readersUI.mode!=="password")return;
  const form=e.currentTarget;
  const body={password:form.elements.password.value,
              admin_password:form.elements.admin_password.value};
  try{
    await api("/admin/readers/"+doctor.id+"/password",{method:"POST",body});
    closeReaderEditors();await refreshAdmin();
    message("密码已重置，该医生原有登录会话已失效");
  }catch(err){form.elements.admin_password.value="";message(err.message,true);}
};

async function refreshAdmin() {
  const [info,doctors,progress]=await Promise.all([
    api("/admin/study"),api("/admin/readers"),api("/admin/progress")
  ]);
  el("study-status").textContent="状态："+info.status+" · 随机摘要："+(info.randomization_digest||"未随机");
  const cards=[["病例数",info.patients+"/"+info.target],["医生",info.readers],
    ["任务",info.tasks],["研究状态",info.status]];
  el("admin-metrics").replaceChildren(...cards.map(([name,value])=>{
    const div=document.createElement("div");div.className="metric";
    const num=document.createElement("strong");num.textContent=value;
    const caption=document.createElement("span");caption.textContent=name;
    div.append(num,caption);return div;
  }));
  renderReaderManager(doctors,info);
  const counts=new Map();
  for(const entry of progress.by_arm) {
    const row=counts.get(entry.arm)||{arm:entry.arm,assigned:0,opened:0,submitted:0};
    row[entry.status]=entry.total;counts.set(entry.arm,row);
  }
  el("progress-table").replaceChildren(smallTable([...counts.values()],
    [["arm","分组（仅管理员）"],["assigned","未开始"],["opened","已打开"],["submitted","已完成"]]));
  const draft=info.status==="draft";
  el("reader-form").querySelector("button").disabled=!draft||info.tasks>0;
  await refreshLaunch();
  await loadDatasetStatus();
  await loadDatasetPage();
  el("lock-button").disabled=info.status!=="published";
  el("export-button").disabled=info.status!=="locked";
  visible("test-reset-panel", !!info.test_reset_enabled && info.status!=="locked");
  el("test-reset-button").disabled=info.tasks===0 && info.patients===0 && !datasetUI.frozen;
  visible("end-round-button",info.status==="published");
  await refreshRoundArchives();

}
el("admin-refresh").onclick=()=>refreshAdmin().catch(err=>message(err.message,true));
el("progress-refresh").onclick=()=>refreshAdmin().catch(err=>message(err.message,true));
el("reader-form").onsubmit=async e=>{
  e.preventDefault();
  const form=e.currentTarget;
  try{await api("/admin/readers",{method:"POST",body:formData(form)});
    form.reset();message("医生账户已创建");await refreshAdmin();}
  catch(err){message(err.message,true);}
};
async function refreshLaunch(){
  const data=await api("/admin/dataset/launch/status");
  el("launch-summary").textContent="拟纳入 "+data.included+" 例 · 已启用医生 "+data.doctors+" 人 · 已分配 "+data.tasks+" 例";
  const published=data.stage==="published";
  const locked=data.stage==="locked";
  el("launch-button").disabled=!data.ready;
  el("launch-button").textContent=published?"已发布，医生可以开始阅片":locked?"研究已锁定":"一键分配并发布任务";
  el("launch-errors").textContent=data.errors?.length?data.errors.join("；"):(published?data.reason:"系统将自动完成队列锁定、随机分配及发布");
}
el("launch-button").onclick=async()=>{
  const state=await api("/admin/dataset/launch/status");
  if(!state.ready){message(state.errors?.join("；")||"当前不可发布",true);await refreshLaunch();return;}
  if(!confirm("将 "+state.included+" 例患者分配给 "+state.doctors+" 名医生，并正式发布。确认后本轮队列和随机分组不可修改。是否继续？"))return;
  const btn=el("launch-button");btn.disabled=true;
  try{
    const result=await api("/admin/dataset/launch",{method:"POST"});
    el("launch-result").textContent="发布成功："+result.tasks+" 例，分组 "+JSON.stringify(result.arms);
    await refreshAdmin();message("阅片任务已发布，医生现在可以登录开始阅片");
  }catch(e){message(e.message,true);await refreshLaunch();}
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

async function refreshRoundArchives(){
  const archives=await api("/admin/round-archives");
  const wrapper=el("round-archives");
  if(!archives.length){
    wrapper.textContent="尚无已结束的历史轮次";
    return;
  }
  const table=document.createElement("table");table.className="simple-table";
  const header=document.createElement("tr");
  ["历史轮次","结束时间","完成/分配","操作"].forEach(label=>{
    const th=document.createElement("th");th.textContent=label;header.append(th);
  });
  const thead=document.createElement("thead");thead.append(header);table.append(thead);
  const body=document.createElement("tbody");
  for(const item of archives){
    const tr=document.createElement("tr");
    for(const value of ["第 "+item.id+" 轮",
      new Date(item.ended_at*1000).toLocaleString(),
      item.completed+" / "+item.assigned]){
      const td=document.createElement("td");td.textContent=value;tr.append(td);
    }
    const actions=document.createElement("td");
    const button=document.createElement("button");
    button.type="button";button.className="quiet";button.textContent="下载本轮CSV";
    button.onclick=async()=>{
      try{
        const blob=await api("/admin/round-archives/"+item.id+"/export.csv",{binary:true});
        const url=URL.createObjectURL(blob),a=document.createElement("a");
        a.href=url;a.download="rct_round_"+item.id+"_partial.csv";a.click();
        setTimeout(()=>URL.revokeObjectURL(url),1500);
      }catch(err){message(err.message,true);}
    };
    actions.append(button);tr.append(actions);body.append(tr);
  }
  table.append(body);wrapper.replaceChildren(table);
}
el("end-round-button").onclick=async()=>{
  if(!confirm("确认强制结束当前轮次？所有未完成阅片将立即停止；已完成诊断和全部任务分配会永久归档，随后可以重新选择病例并发布下一轮。"))return;
  const password=prompt("请输入管理员密码，确认结束并归档当前轮次：","");
  if(!password)return;
  const button=el("end-round-button");button.disabled=true;
  try{
    const result=await api("/admin/end-round",{method:"POST",body:{
      confirm:"END CURRENT ROUND",admin_password:password,
      reason:"管理员手动提前结束本轮阅片"
    }});
    datasetUI.page=1;datasetUI.selected.clear();datasetUI.preview=null;
    visible("dataset-preview",false);
    el("launch-result").textContent="";
    await refreshAdmin();
    message("第 "+result.archive_id+" 轮已归档：完成 "+result.completed+" 例、未完成 "+result.incomplete+" 例。可以开始发布下一轮。");
  }catch(err){message(err.message,true);}
  finally{button.disabled=false;}
};

el("test-reset-button").onclick=async()=>{
  const typed=prompt("此操作将清除本轮研究任务及所有诊断结果（会先备份数据库）。\n请输入 RESET TEST STUDY 确认：","");
  if(typed!=="RESET TEST STUDY"){if(typed!==null)message("确认文字不匹配，未执行重置",true);return;}
  const password=prompt("请输入当前管理员密码以确认重置：","");
  if(!password)return;
  const button=el("test-reset-button");
  button.disabled=true;
  try{
    const result=await api("/admin/reset-test-study",{method:"POST",body:{confirm:typed,password}});
    el("launch-result").textContent="";
    datasetUI.page=1;datasetUI.selected.clear();datasetUI.preview=null;
    visible("dataset-preview",false);
    await refreshAdmin();
    message("研究已恢复草稿。清除任务 "+result.removed_tasks+"、结果 "+result.removed_results+"；备份："+result.backup_name);
  }catch(e){message(e.message,true);}
  finally{button.disabled=false;}
};


/* Candidate file index; never communicates source labels or paths to reader views. */
const datasetUI={page:1,pageSize:50,total:0,selected:new Set(),frozen:false,preview:null,poll:null};
async function loadDatasetStatus(){
  const data=await api("/admin/dataset/status");
  datasetUI.frozen=!!data.scan.frozen_at||data.study_status!=="draft";
  const counts=data.counts||{};
  el("dataset-counts").textContent="发现 "+(counts.total||0)+" · 拟纳入 "+(counts.included||0)+" · 排除 "+(counts.excluded||0)+" · 缺失 "+(counts.missing||0)+" · 无法读取 "+(counts.unreadable||0);
  el("dataset-scan-status").textContent="扫描："+data.scan.status+
    (data.scan.last_finished?" · 最近完成："+new Date(data.scan.last_finished*1000).toLocaleString():"")+
    (data.scan.last_error?" · "+data.scan.last_error:"");
  el("dataset-exclude").disabled=datasetUI.frozen;
  el("dataset-restore").disabled=datasetUI.frozen;
  clearTimeout(datasetUI.poll);
  if(data.scan.status==="running"){
    datasetUI.poll=setTimeout(()=>refreshAdmin().catch(e=>message(e.message,true)),1500);
  }
  return data;
}
function datasetTextCell(row,text){const td=document.createElement("td");td.textContent=String(text??"—");row.append(td);return td;}
async function loadDatasetPage(){
  datasetUI.selected.clear();
  const args=new URLSearchParams({page:String(datasetUI.page),page_size:String(datasetUI.pageSize),
    query:el("dataset-query").value.trim(),filter:el("dataset-filter").value});
  const data=await api("/admin/dataset/files?"+args.toString());
  datasetUI.total=data.total;
  const table=document.createElement("table");table.className="simple-table";
  const head=document.createElement("tr");
  ["选择","Patient ID","文件名","标签","切片","纳入","文件状态","操作"].forEach(name=>{
    const th=document.createElement("th");th.textContent=name;head.append(th);
  });
  const th=document.createElement("thead");th.append(head);table.append(th);
  const body=document.createElement("tbody");
  for(const item of data.items){
    const tr=document.createElement("tr");
    const choose=document.createElement("td");const checkbox=document.createElement("input");
    checkbox.type="checkbox";checkbox.disabled=datasetUI.frozen;
    checkbox.onchange=()=>checkbox.checked?datasetUI.selected.add(item.id):datasetUI.selected.delete(item.id);
    choose.append(checkbox);tr.append(choose);
    datasetTextCell(tr,item.patient_code);
    datasetTextCell(tr,item.filename);
    datasetTextCell(tr,item.label_type);
    datasetTextCell(tr,item.depth);
    datasetTextCell(tr,item.included?"拟纳入":"已排除");
    datasetTextCell(tr,item.missing?"Missing":item.error||"Indexed");
    const actions=document.createElement("td");const preview=document.createElement("button");
    preview.className="quiet";preview.textContent="查看";
    preview.onclick=()=>openDatasetPreview(item);
    actions.append(preview);tr.append(actions);body.append(tr);
  }
  table.append(body);el("dataset-table").replaceChildren(table);
  const pages=Math.max(1,Math.ceil(data.total/datasetUI.pageSize));
  el("dataset-page").textContent="第 "+datasetUI.page+" / "+pages+" 页，共 "+data.total+" 例";
  el("dataset-prev").disabled=datasetUI.page<=1;
  el("dataset-next").disabled=datasetUI.page>=pages;
}
el("dataset-prev").onclick=async()=>{datasetUI.page--;try{await loadDatasetPage();}catch(e){message(e.message,true);}};
el("dataset-next").onclick=async()=>{datasetUI.page++;try{await loadDatasetPage();}catch(e){message(e.message,true);}};
el("dataset-search").onclick=async()=>{datasetUI.page=1;try{await loadDatasetPage();}catch(e){message(e.message,true);}};
el("dataset-query").onkeydown=e=>{if(e.key==="Enter"){e.preventDefault();el("dataset-search").click();}};
el("dataset-filter").onchange=()=>el("dataset-search").click();
el("dataset-scan").onclick=async()=>{
  try{await api("/admin/dataset/scan",{method:"POST"});message("已启动目录扫描");await loadDatasetStatus();}
  catch(e){message(e.message,true);}
};
async function updateDatasetSelection(included){
  const ids=[...datasetUI.selected];
  if(!ids.length){message("请先勾选病例",true);return;}
  if(datasetUI.frozen){message("队列已冻结",true);return;}
  let reason=null;
  if(!included){reason=prompt("排除原因（可留空）","");if(reason===null)return;}
  try{
    await api("/admin/dataset/selection",{method:"POST",body:{ids,included,reason}});
    message("已更新 "+ids.length+" 例");
    await loadDatasetStatus();await loadDatasetPage();await refreshLaunch();
  }catch(e){message(e.message,true);}
}
el("dataset-exclude").onclick=()=>updateDatasetSelection(false);
el("dataset-restore").onclick=()=>updateDatasetSelection(true);
el("dataset-preview-close").onclick=()=>{datasetUI.preview=null;visible("dataset-preview",false);};
async function openDatasetPreview(item){
  if(item.missing||item.error||!item.depth){message("此文件元数据不可用",true);return;}
  datasetUI.preview={id:item.id,z:Math.floor(item.depth/2),depth:item.depth,revision:0};
  el("dataset-preview-title").textContent=item.patient_code+" · 影像预览";
  visible("dataset-preview",true);await drawDatasetPreview();
}
async function drawDatasetPreview(){
  const p=datasetUI.preview;if(!p)return;
  const seq=++p.revision;
  const phaseNames=["NCCT","REAL_AP","REAL_PVP","REAL_DP","SYN_AP","SYN_PVP","SYN_DP"];
  el("dataset-preview-z").textContent=(p.z+1)+" / "+p.depth;
  const grid=el("dataset-preview-grid");grid.replaceChildren();
  await Promise.all(phaseNames.map(async phase=>{
    const tile=document.createElement("div");tile.className="phase";
    const caption=document.createElement("span");caption.textContent=phase.replace("_"," ");
    const img=document.createElement("img");img.alt=phase;
    tile.append(caption,img);grid.append(tile);
    try{
      const blob=await api("/admin/dataset/files/"+p.id+"/images/"+phase+"/"+p.z,{binary:true});
      if(datasetUI.preview!==p||p.revision!==seq)return;
      const url=URL.createObjectURL(blob);img.onload=()=>URL.revokeObjectURL(url);
      img.src=url;
    }catch(e){caption.textContent=phase+" · 不可用";}
  }));
}
function stepDatasetPreview(d){
  const p=datasetUI.preview;if(!p)return;
  p.z=Math.max(0,Math.min(p.depth-1,p.z+d));drawDatasetPreview();
}
el("dataset-preview-prev").onclick=()=>stepDatasetPreview(-1);
el("dataset-preview-next").onclick=()=>stepDatasetPreview(1);
el("dataset-preview-grid").addEventListener("wheel",e=>{e.preventDefault();stepDatasetPreview(e.deltaY>0?1:-1);},{passive:false});

async function refreshReader(){
  const data=await api("/tasks/summary");
  el("reader-summary").textContent="已完成 "+data.completed+"/"+data.total+" · 剩余 "+data.remaining;
  if(data.remaining===0) {visible("reader-idle",true);el("reader-idle").textContent="已完成全部评阅任务。";}
}
// The viewer swaps complete, decoded phase groups in a single animation frame.
// Cache requests (and decoded images) so a rapid wheel gesture does not refetch.
const viewer = {
  cache: new Map(), pending: new Map(), shown: null,
  frame: null, prefetchTimer: null, wheel: 0, wheelFrame: null,
  wheelAt: 0, sliceEpoch: 0
};
function discardImages() {
  study.generation++;
  viewer.sliceEpoch++;
  if(viewer.frame!==null)cancelAnimationFrame(viewer.frame);
  if(viewer.wheelFrame!==null)cancelAnimationFrame(viewer.wheelFrame);
  clearTimeout(viewer.prefetchTimer);
  viewer.frame=null;viewer.wheelFrame=null;viewer.wheel=0;viewer.shown=null;
  viewer.pending.clear();
  for(const entry of viewer.cache.values())URL.revokeObjectURL(entry.url);
  viewer.cache.clear();
  study.images.clear();study.expanded=null;study.transforms.clear();
}
async function startNext(){
  if(study.busy)return;
  try {
    const response=await api("/tasks/next");
    if(!response.task){study.task=null;discardImages();visible("reading",false);visible("reader-idle",true);
      el("reader-idle").textContent="当前没有待评阅任务。";await refreshReader();return;}
    discardImages();
    const task=await api("/tasks/"+response.task.id);
    study.task=task;study.z=Math.floor(task.depth/2);study.lo=-160;study.hi=240;
    el("case-number").textContent=task.public_code;
    el("reading-status").textContent="单次独立诊断 · 阅片中";
    el("diagnosis-form").reset();syncDiagnosisFields();
    visible("reading",true);visible("reader-idle",false);
    const grid=el("phase-grid");grid.replaceChildren();
    task.phases.forEach(phase=>{
      const pane=document.createElement("div");pane.className="phase";
      pane.dataset.phase=phase;
      const heading=document.createElement("span");heading.textContent=phase;
      const img=document.createElement("img");img.alt=phase+" CT";
      img.draggable=false;
      pane.append(heading,img);
      pane.addEventListener("dblclick",event=>{
        event.preventDefault();
        study.expanded=study.expanded===phase?null:phase;
        document.querySelectorAll("#phase-grid .phase").forEach(node=>node.classList.toggle("expanded",node.dataset.phase===study.expanded));
        grid.classList.toggle("focus",!!study.expanded);
        if(study.expanded)setZoom(phase,Math.max(1.7,transformFor(phase).zoom));
      });
      let drag=null;
      pane.addEventListener("pointerdown",event=>{
        if(event.button!==0)return;
        const t=transformFor(phase);
        drag={x:event.clientX,y:event.clientY,panX:t.x,panY:t.y};
        pane.setPointerCapture(event.pointerId);
      });
      pane.addEventListener("pointermove",event=>{
        if(!drag)return;
        const t=transformFor(phase);
        t.x=drag.panX+event.clientX-drag.x;
        t.y=drag.panY+event.clientY-drag.y;
        applyTransform(phase);
      });
      const endDrag=()=>{drag=null;};
      pane.addEventListener("pointerup",endDrag);
      pane.addEventListener("pointercancel",endDrag);
      pane.addEventListener("lostpointercapture",endDrag);
      grid.append(pane);
    });
    study.startTime=Date.now();
    await renderImages();
  }catch(err){message(err.message,true);}
}
el("continue-button").onclick=startNext;
function imageKey(phase,z,task=study.task){
  return [task.id,phase,z,study.lo,study.hi].join("|");
}
async function cachedSlice(task,phase,z,epoch){
  const key=imageKey(phase,z,task);
  if(viewer.cache.has(key)){
    const item=viewer.cache.get(key);
    viewer.cache.delete(key);viewer.cache.set(key,item); // LRU touch
    return item;
  }
  if(viewer.pending.has(key))return viewer.pending.get(key);
  const promise=(async()=>{
    const blob=await api("/tasks/"+task.id+"/images/"+phase+"/"+z+
      "?lo="+encodeURIComponent(study.lo)+"&hi="+encodeURIComponent(study.hi),{binary:true});
    const url=URL.createObjectURL(blob);
    try{
      // Decode before swapping: otherwise the browser briefly paints a blank tile.
      const preload=new Image();
      preload.src=url;
      if(preload.decode)await preload.decode();
      else await new Promise((resolve,reject)=>{
        if(preload.complete){preload.naturalWidth?resolve():reject(new Error("Invalid CT image"));return;}
        preload.onload=resolve;preload.onerror=()=>reject(new Error("Invalid CT image"));
      });
      if(epoch!==study.generation||study.task?.id!==task.id){
        URL.revokeObjectURL(url);return null;
      }
      const item={url};
      viewer.cache.set(key,item);
      trimImageCache(task);
      return item;
    }catch(err){URL.revokeObjectURL(url);throw err;}
  })();
  viewer.pending.set(key,promise);
  try{return await promise;}
  finally{if(viewer.pending.get(key)===promise)viewer.pending.delete(key);}
}
function trimImageCache(task){
  // Keep current and adjacent slices. Never revoke a URL currently painted.
  const pinned=new Set();
  if(viewer.shown!==null){
    for(const phase of task.phases)pinned.add(imageKey(phase,viewer.shown,task));
  }
  // Protect the new target and any currently requested image from LRU eviction.
  for(const phase of task.phases)pinned.add(imageKey(phase,study.z,task));
  for(const key of viewer.pending.keys())pinned.add(key);
  if(viewer.cache.size<=72)return;
  for(const [key,item] of viewer.cache){
    if(viewer.cache.size<=48)break;
    if(!pinned.has(key)){
      URL.revokeObjectURL(item.url);viewer.cache.delete(key);
    }
  }
}
async function renderImages(){
  if(!study.task)return;
  const epoch=study.generation, request=++viewer.sliceEpoch;
  const task=study.task,z=study.z;
  el("slice-position").textContent=(z+1)+" / "+task.depth;
  const phaseImages=await Promise.allSettled(task.phases.map(async phase=>({
    phase,item:await cachedSlice(task,phase,z,epoch)
  })));
  if(epoch!==study.generation||task!==study.task||request!==viewer.sliceEpoch)return;
  const failed=phaseImages.find(p=>p.status!=="fulfilled"||!p.value.item);
  if(failed){
    // Keep the last fully rendered phase group instead of clearing individual tiles.
    message("切片加载失败，保留上一帧。请稍后重试。",true);
    return;
  }
  if(viewer.frame!==null)cancelAnimationFrame(viewer.frame);
  viewer.frame=requestAnimationFrame(()=>{
    viewer.frame=null;
    if(epoch!==study.generation||task!==study.task||request!==viewer.sliceEpoch)return;
    for(const {value:{phase,item}} of phaseImages){
      const img=[...document.querySelectorAll("#phase-grid .phase")].find(node=>node.dataset.phase===phase)?.querySelector("img");
      if(img&&img.src!==item.url)img.src=item.url;
    }
    viewer.shown=z;
    trimImageCache(task);
    clearTimeout(viewer.prefetchTimer);
    // Prefetch only after current slice is shown and the user briefly stops.
    viewer.prefetchTimer=setTimeout(()=>{
      if(epoch!==study.generation||task!==study.task||request!==viewer.sliceEpoch)return;
      for(const offset of [-1,1]){
        const neighbor=z+offset;
        if(neighbor<0||neighbor>=task.depth)continue;
        for(const phase of task.phases){
          cachedSlice(task,phase,neighbor,epoch).catch(()=>{});
        }
      }
    },100);
  });
}
function moveSlice(delta){
  if(!study.task||el("reading").hidden)return;
  const next=Math.min(study.task.depth-1,Math.max(0,study.z+delta));
  if(next===study.z)return;
  study.z=next;
  renderImages();
}
function transformFor(phase){
  if(!study.transforms.has(phase))study.transforms.set(phase,{zoom:1,x:0,y:0});
  return study.transforms.get(phase);
}
function applyTransform(phase){
  const pane=[...document.querySelectorAll(".phase")].find(node=>node.dataset.phase===phase);
  const img=pane?.querySelector("img");if(!img)return;
  const t=transformFor(phase);
  img.style.transform="translate("+t.x+"px, "+t.y+"px) scale("+t.zoom+")";
}
function setZoom(phase,value){
  const t=transformFor(phase);
  t.zoom=Math.max(1,Math.min(6,value));
  if(t.zoom===1){t.x=0;t.y=0;}
  applyTransform(phase);
}
el("phase-grid").addEventListener("wheel",e=>{
  if(!study.task||el("reading").hidden)return;
  e.preventDefault();
  const pane=e.target.closest(".phase");
  if(e.ctrlKey||e.metaKey||e.shiftKey){
    if(pane)setZoom(pane.dataset.phase,transformFor(pane.dataset.phase).zoom*(e.deltaY<0?1.15:1/1.15));
    return;
  }
  // Normalize browser wheel modes, then coalesce touchpad bursts per frame.
  const amount=e.deltaY*(e.deltaMode===1?16:e.deltaMode===2?480:1);
  const now=performance.now();
  if(now-viewer.wheelAt>180)viewer.wheel=0;
  viewer.wheelAt=now;
  viewer.wheel+=amount;
  if(viewer.wheelFrame!==null)return;
  viewer.wheelFrame=requestAnimationFrame(()=>{
    viewer.wheelFrame=null;
    const steps=Math.trunc(viewer.wheel/55);
    if(!steps)return;
    viewer.wheel-=steps*55;
    moveSlice(Math.max(-3,Math.min(3,steps)));
  });
},{passive:false});
document.addEventListener("keydown",e=>{
  if(!study.task||el("reading").hidden||["INPUT","TEXTAREA","SELECT"].includes(document.activeElement?.tagName))return;
  if(e.key==="ArrowUp"||e.key==="ArrowDown"){e.preventDefault();moveSlice(e.key==="ArrowDown"?1:-1);}
});
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
