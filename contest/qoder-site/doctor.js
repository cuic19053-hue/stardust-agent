'use strict';
/* Pure consultation helpers, shared by the page and by Node tests. */
const VERDICTS={agree:'赞同',challenge:'质疑',uncertain:'待核'};
function splitDoc(s){
  const chunks=[]; let start=0;
  while(start<s.length){let end=Math.min(start+2000,s.length); if(end<s.length){const cut=s.lastIndexOf('\n',end);if(cut>start+600)end=cut+1;} chunks.push(s.slice(start,end));start=end;}
  return chunks;
}
function buildRoleSample(content, limit){
  limit=limit||3600;
  if(content.length<=limit) return content;
  const head=content.slice(0,600), tail=content.slice(-600), excerpt=150;
  const heading=/^(?:[一二三四五六七八九十百]{1,3}|\d{1,2})[、.)）．]\s*\S/;
  const marks=[]; let offset=0;
  for(const line of content.split('\n')){
    if(heading.test(line) && offset>600 && offset<content.length-600 && (!marks.length || offset>=marks[marks.length-1]+excerpt)) marks.push(offset);
    offset+=line.length+1;
  }
  const maxCount=Math.max(0,Math.floor((limit-1200)/excerpt));
  const step=Math.max(1,Math.ceil(marks.length/maxCount||1));
  const picked=[];
  for(let i=0;i<marks.length && picked.length<maxCount;i+=step) picked.push(content.slice(marks[i],marks[i]+excerpt));
  if(!picked.length) picked.push(content.slice(Math.floor(content.length/2)-600,Math.floor(content.length/2)+600));
  return [head,...picked,tail].join('\n');
}
function reviewerIndex(count, roleIndex, findingIndex){
  if(count<2) return -1;
  let j=(roleIndex+1+findingIndex)%count;
  if(j===roleIndex) j=(j+1)%count;
  return j;
}
const consult={VERDICTS,splitDoc,buildRoleSample,reviewerIndex};
if (typeof module!=='undefined' && module.exports) module.exports=consult;
if (typeof globalThis!=='undefined') globalThis.DoctorConsult=consult;

if (typeof document!=='undefined') (() => {
  const el=id=>document.getElementById(id), text=el('documentText'), issuesBox=el('doctorIssues');
  const esc=value=>String(value).replace(/[&<>"']/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
  const demo='【模拟项目：校园报修文本分类】\n\n一、研究目的\n本项目拟研究校园报修文本的分类方法，减少人工分拣的重复工作。我们将先了解报修流程，再比较不同分类方法。\n\n二、研究内容\n研究将收集经授权的报修文本，整理类别和标注规范，构建训练和评估流程。数据来源、样本数量和授权方式尚未确认。\n\n三、创新点\n本项目具有创新性，采用人工智能技术建设一个智能系统，提升校园服务水平。\n\n四、实施计划\n第一阶段完成需求分析与标注规范，第二阶段开展模型实验并制作原型。具体周期和计算资源有待团队确认。\n\n五、预期成果\n本项目已实测，分类准确率达到 98.6%。目前团队尚未开始实验，结果尚待测试。';
  let revision=0, reportRevision=-1, report=[], failures=[], busy=false, roles=[], roleRevision=0, reportRoles=[];
  const setProgress=message=>{el('doctorProgress').textContent=message};
  function count(){el('documentCount').textContent=text.value.length+' / 16000 字'}
  function change(){ revision++; count(); if(roles.length) el('roleMessage').textContent='正文已更改，请检查原先推荐的视角是否仍适用。'; if(report.length) setProgress('材料已更改：现有报告对应旧版本。请对具体修改复查，或重新会诊整份材料。'); }
  text.addEventListener('input',change); count();
  async function api(path, payload){
    let response;
    try {response=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});} catch {throw Error('无法连接本机服务，请先启动服务。')}
    const data=await response.json().catch(()=>({}));
    if(!response.ok) throw Error(typeof data.detail==='string'?data.detail:'处理失败，请重试。');
    return data;
  }
  function renderRoles(){
    el('roleList').innerHTML=roles.map((r,i)=>'<div class="role-row"><label><input type="checkbox" data-role-check="'+i+'" '+(r.selected?'checked':'')+'>启用</label><label>名称<input type="text" maxlength="20" data-role-name="'+i+'" value="'+esc(r.name)+'"></label><label>关注点<input type="text" maxlength="80" data-role-focus="'+i+'" value="'+esc(r.focus)+'"></label><button type="button" class="finding-jump" data-role-remove="'+i+'">移除</button>'+(r.basis_quote?'<p class="helper-text">推荐依据：'+esc(r.basis_quote)+'</p>':'')+'</div>').join('');
  }
  el('roleList').addEventListener('input',event=>{
    const d=event.target.dataset;
    roleRevision++;
    if(d.roleName!==undefined) roles[Number(d.roleName)].name=event.target.value;
    if(d.roleFocus!==undefined) roles[Number(d.roleFocus)].focus=event.target.value;
    if(d.roleCheck!==undefined) roles[Number(d.roleCheck)].selected=event.target.checked;
  });
  el('roleList').addEventListener('click',event=>{const b=event.target.closest('[data-role-remove]');if(!b)return;roleRevision++;roles.splice(Number(b.dataset.roleRemove),1);renderRoles();});
  el('addRole').addEventListener('click',()=>{if(roles.length>=5){el('roleMessage').textContent='当前版本最多支持 5 个视角。';return;} roleRevision++;roles.push({name:'',focus:'',basis_quote:'',selected:true});renderRoles();});
  async function planRoles(){
    const content=text.value;
    if(!content.trim()){el('roleMessage').textContent='请先导入或粘贴材料。';return false;}
    if(content.length>16000){el('roleMessage').textContent='正文超过 16000 字，请分章处理。';return false;}
    if(busy)return false;
    const version=revision, rolesVersion=roleRevision;
    const sample=buildRoleSample(content);
    busy=true;el('planRoles').disabled=true;el('roleMessage').textContent='正在根据材料提出视角…';
    try{
      const result=await api('/api/plan-roles',{sample});
      if(version!==revision){el('roleMessage').textContent='正文已改变，旧的视角建议已丢弃。';return false;}
      if(rolesVersion!==roleRevision){el('roleMessage').textContent='视角已手动调整，本次推荐未覆盖你的修改。';return false;}
      if(result.roles.some(r=>!content.includes(r.basis_quote))) throw Error('角色推荐依据无法在完整材料中定位，请重试。');
      roles=result.roles.map(r=>({...r,selected:true}));roleRevision++;renderRoles();
      el('roleMessage').textContent=content.length>3600?'长文按章节标题抽样后推荐了 '+result.roles.length+' 个视角；可修改、增删或取消勾选。':'基于当前材料推荐了 '+result.roles.length+' 个视角；可修改、增删或取消勾选。';return true;
    }catch(error){el('roleMessage').textContent=error.message+' 你也可以手动添加视角。';return false;}
    finally{busy=false;el('planRoles').disabled=false;}
  }
  el('planRoles').addEventListener('click',planRoles);
  function render(){
    issuesBox.innerHTML=report.map((x,i)=>'<article class="finding doctor-issue"><div class="finding-top"><span class="finding-title">'+esc(x.role_name)+' · 问题 '+(i+1)+(x.applied?'（已采用替换）':'')+'</span><span class="status status-pending">需人工复核</span></div><div class="doctor-quote"><b>定位原文</b><p>'+esc(x.quote)+'</p></div><p><strong>问题：</strong>'+esc(x.problem)+'</p><p><strong>建议：</strong>'+esc(x.suggestion)+'</p>'+(x.question?'<p><strong>待补充：</strong>'+esc(x.question)+'</p>':'')+(x.cross?'<div class="cross-note"><b>'+esc(x.cross.reviewer)+' · 交叉复核：'+esc(VERDICTS[x.cross.verdict]||x.cross.verdict)+'</b><p>'+esc(x.cross.reason)+'</p>'+(x.cross.quote?'<p>依据原文：'+esc(x.cross.quote)+'</p>':'<p>未找到可引用原文，结论仅为待核。</p>')+'</div>':'<p class="helper-text">交叉复核：'+(busy?'等待处理':'未完成或仅选择一个视角')+'</p>')+'<div class="finding-actions"><button type="button" class="finding-jump" data-action="locate" data-index="'+i+'">定位原文 ↗</button><button type="button" class="finding-jump" data-action="rewrite" data-index="'+i+'" '+(busy?'disabled':'')+'>生成修改建议 ↗</button></div><label class="revision-label" for="revision-'+i+'">修改后片段（可自行编辑）</label><textarea id="revision-'+i+'" data-revision="'+i+'" rows="4" '+(busy?'disabled':'')+' placeholder="生成建议或自行填写修改后的片段">'+esc(x.revised||'')+'</textarea><p class="helper-text">建议可能包含不准确内容；请核实事实，替换仅作用于唯一匹配的原文。</p><div class="doctor-actions"><button type="button" class="button button-quiet" data-action="recheck" data-index="'+i+'" '+(busy?'disabled':'')+'>复查此问题</button><button type="button" class="button button-quiet" data-action="apply" data-index="'+i+'" '+(busy?'disabled':'')+'>采用并替换原文</button></div><p class="helper-text" data-feedback="'+i+'">'+esc(x.feedback||'')+'</p></article>').join('');
    el('downloadDoctor').disabled=report.length===0 || busy;
  }
  function feedback(i,value){ report[i].feedback=value; const node=issuesBox.querySelector('[data-feedback="'+i+'"]'); if(node)node.textContent=value; }
  async function diagnose(){
    if(busy)return;
    const documentValue=text.value;
    if(!documentValue.trim()){setProgress('请先导入或粘贴申报书正文。'); text.focus(); return;}
    if(documentValue.length>16000){setProgress('正文超过 16000 字，请分章处理。');return;}
    if(location.protocol==='file:'){setProgress('请先启动本机服务，再打开 http://127.0.0.1:8765/。');return;}
    if(!roles.length && !(await planRoles())){setProgress('请先生成或手动添加诊断视角。');return;}
    const selected=roles.filter(r=>r.selected).map(r=>({name:r.name.trim(),focus:r.focus.trim()}));
    if(!selected.length || selected.some(r=>!r.name||!r.focus)){setProgress('请至少选择一个视角，并填写名称和关注点。');return;}
    if(new Set(selected.map(r=>r.name)).size!==selected.length){setProgress('请使用不同的视角名称，以便交叉复核。');return;}
    if(documentValue!==text.value){setProgress('材料已改变，请重新开始。');return;}
    const chunks=splitDoc(documentValue), version=revision, total=chunks.length*selected.length;
    report=[]; failures=[]; reportRoles=selected; reportRevision=version; busy=true; el('diagnoseDocument').disabled=true; render();
    let done=0;
    for(let c=0;c<chunks.length;c++) for(let r=0;r<selected.length;r++){
      if(version!==revision){failures.push('材料发生变化，后续初诊未完成。');setProgress('材料在会诊期间发生变化，已停止后续诊断。请重新开始。'); busy=false;el('diagnoseDocument').disabled=false;render();return;}
      const role=selected[r];
      setProgress('第一轮：第 '+(c+1)+' / '+chunks.length+' 段 · '+role.name+'（'+done+' / '+total+'）');
      try{
        const data=await api('/api/diagnose',{chunk:chunks[c],role:'custom',custom_focus:role.focus});
        if(version!==revision){setProgress('材料在会诊期间发生变化，旧结果已丢弃。');report=[];busy=false;render();el('diagnoseDocument').disabled=false;return;}
        data.issues.forEach(item=>{if(!report.some(x=>x.role_index===r&&x.quote===item.quote))report.push({...item,role_name:role.name,role_index:r,context:chunks[c],revised:'',feedback:'',cross:null});}); render();
      }catch(error){failures.push('第 '+(c+1)+' 段 / '+role.name+'：'+error.message)}
      done++;
    }
    if(version!==revision){failures.push('材料发生变化，后续会诊未完成。');setProgress('材料已变化，请重新会诊。');busy=false;el('diagnoseDocument').disabled=false;render();return;}
    if(selected.length>1){
      for(let i=0;i<report.length;i++){
        if(version!==revision){failures.push('材料发生变化，交叉复核未完成。');setProgress('材料已变化，已停止交叉复核；请重新会诊。');busy=false;el('diagnoseDocument').disabled=false;render();return;}
        const item=report[i], reviewerSlot=reviewerIndex(selected.length,item.role_index,i);
        if(reviewerSlot<0){failures.push('问题 '+(i+1)+' 未能安排其他视角复核。');continue;}
        const reviewer=selected[reviewerSlot];
        setProgress('第二轮：'+reviewer.name+' 复核问题 '+(i+1)+' / '+report.length);
        try{
          const data=await api('/api/cross-review',{context:item.context,original_quote:item.quote,problem:item.problem,suggestion:item.suggestion,author:item.role_name,reviewer:reviewer.name,reviewer_focus:reviewer.focus});
          if(version!==revision){failures.push('材料发生变化，交叉复核未完成。');setProgress('材料已变化，交叉意见已丢弃；请重新会诊。');busy=false;el('diagnoseDocument').disabled=false;render();return;}
          item.cross={...data,reviewer:reviewer.name};render();
        }catch(error){failures.push('问题 '+(i+1)+' / '+reviewer.name+' 交叉复核：'+error.message)}
      }
    }
    if(version!==revision){failures.push('材料发生变化，本次会诊未完成。');setProgress('材料已变化，请重新会诊。');busy=false;el('diagnoseDocument').disabled=false;render();return;}
    busy=false;el('diagnoseDocument').disabled=false;render();
    const tally=report.reduce((acc,x)=>{if(x.cross)acc[x.cross.verdict]=(acc[x.cross.verdict]||0)+1;return acc;},{});
    const crossSummary=selected.length>1&&report.some(x=>x.cross)?' 交叉结论：赞同 '+(tally.agree||0)+'、质疑 '+(tally.challenge||0)+'、待核 '+(tally.uncertain||0)+'。':'';
    setProgress('会诊完成：'+report.length+' 条意见，'+report.filter(x=>x.cross).length+' 条已交叉复核，'+failures.length+' 项未完成。'+crossSummary+(selected.length===1?' 仅选择一个视角，未进行交叉复核。':failures.length?' '+failures.join('；'):' 没有发现不等于没有问题，请结合全文复核。'));
  }
  el('diagnoseDocument').addEventListener('click',diagnose);
  el('documentDemo').addEventListener('click',()=>{if(text.value.trim()&&text.value!==demo&&!window.confirm('载入模拟稿会替换当前正文，继续吗？'))return;text.value=demo;change();el('fileMessage').textContent='已载入虚构案例；请先阅读正文。'; text.focus();});
  el('documentFile').addEventListener('change',async event=>{
    const file=event.target.files[0]; if(!file)return;
    if(text.value.trim()&&!window.confirm('导入文件会替换当前正文，继续吗？')){event.target.value='';return;}
    if(file.size>2*1024*1024){el('fileMessage').textContent='文件超过 2 MB，请分文件处理。';event.target.value='';return;}
    const importRevision=revision;
    el('fileMessage').textContent='正在本机提取文字…';
    try{
      const encoded=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(String(reader.result).split(',')[1]);reader.onerror=()=>reject(Error('读取文件失败。'));reader.readAsDataURL(file);});
      const result=await api('/api/extract',{filename:file.name,content:encoded});
      if(revision!==importRevision){el('fileMessage').textContent='正文在提取期间发生变化，已保留当前正文；请重新选择文件。';return;}
      text.value=result.text;change();el('fileMessage').textContent='已提取 '+result.text.length+' 字，请检查正文和表格顺序。';
    }catch(error){el('fileMessage').textContent=error.message;} finally{event.target.value='';}
  });
  issuesBox.addEventListener('input',event=>{const index=event.target.dataset.revision;if(index!==undefined){report[Number(index)].revised=event.target.value;report[Number(index)].feedback='修改内容已更改，请重新复查。';const node=issuesBox.querySelector('[data-feedback="'+index+'"]');if(node)node.textContent=report[Number(index)].feedback;}});
  issuesBox.addEventListener('click',async event=>{
    const button=event.target.closest('[data-action]'); if(!button)return;
    const index=Number(button.dataset.index), item=report[index], action=button.dataset.action; if(!item)return;
    if(action==='locate'){
      const pos=text.value.indexOf(item.quote); if(pos<0){feedback(index,'原文已变化，请重新会诊。');return;}
      text.focus();text.setSelectionRange(pos,pos+item.quote.length);text.scrollIntoView({behavior:'smooth',block:'center'});return;
    }
    if(action==='apply'){
      const revised=(item.revised||'').trim();const first=text.value.indexOf(item.quote);
      if(!revised){feedback(index,'请先生成或填写修改后的片段。');return;}
      if(first<0||text.value.indexOf(item.quote,first+item.quote.length)>=0){feedback(index,'原文不存在或重复出现，请手动定位修改。');return;}
      if(text.value.length-item.quote.length+revised.length>16000){feedback(index,'替换后超过 16000 字，请缩短片段。');return;}
      text.setRangeText(revised,first,first+item.quote.length,'select');change();item.applied=true;feedback(index,'已替换原文。请复查该问题；其他意见基于旧版本。');render();return;
    }
    if(busy){feedback(index,'模型正忙，请稍后重试。');return;}
    if(action==='recheck'&&!(item.revised||'').trim()){feedback(index,'请先填写修改后的片段。');return;}
    if(action==='rewrite'&&(item.revised||'').trim()&&!window.confirm('修改片段已有内容，生成建议将覆盖并替换它，继续吗？'))return;
    const snapshot=(item.revised||'').trim(); busy=true;button.disabled=true;feedback(index,action==='rewrite'?'正在生成修改建议…':'正在复查修改后的片段…');
    try{
      if(action==='rewrite'){
        const crossNote=item.cross?((VERDICTS[item.cross.verdict]||item.cross.verdict)+'（'+item.cross.reviewer+'）：'+item.cross.reason+(item.cross.quote?'；依据原文：'+item.cross.quote:'')).slice(0,400):'';
        const data=await api('/api/rewrite',{original:item.quote,problem:item.problem,suggestion:item.suggestion,cross_note:crossNote});
        if((item.revised||'').trim()!==snapshot){feedback(index,'片段已变化，旧建议未覆盖你的编辑。');return;}
        item.revised=data.rewrite; item.applied=false; const field=el('revision-'+index);if(field)field.value=data.rewrite; feedback(index,data.notice+(crossNote?'（已纳入交叉复核结论）':''));
      }else{
        const data=await api('/api/recheck',{original:item.quote,revised:snapshot,problem:item.problem});
        if((item.revised||'').trim()!==snapshot){feedback(index,'片段已变化，旧复查结果已丢弃。');return;}
        const names={improved:'可能已有改善',unresolved:'问题可能仍在',uncertain:'仍需人工确认'};feedback(index,names[data.status]+'：'+data.reason+'（复查原文：'+data.quote+'）'+(item.applied?'':'（片段尚未替换到正文）'));
      }
    }catch(error){feedback(index,error.message);} finally{busy=false;button.disabled=false;}
  });
  el('downloadDoctor').addEventListener('click',()=>{
    const lines=['立项医生 · 会诊记录','生成时间：'+new Date().toLocaleString('zh-CN'),'材料状态：'+(reportRevision===revision?'与诊断时一致':'已修改，报告基于旧版本'),'会诊视角：'+reportRoles.map(r=>r.name+'（'+r.focus+'）').join('、'),'说明：模型意见仅供人工复核，可能遗漏或误判。',''];
    report.forEach((x,i)=>{lines.push((i+1)+'. '+x.role_name,'原文：'+x.quote,'问题：'+x.problem,'建议：'+x.suggestion,'待补充：'+(x.question||'无'),'交叉复核：'+(x.cross?(x.cross.reviewer+' / '+(VERDICTS[x.cross.verdict]||x.cross.verdict)+' / '+x.cross.reason+' / '+(x.cross.quote||'无引文')):'未完成'),'修改片段：'+(x.revised||'未填写'),'采用状态：'+(x.applied?'已替换原文（复查针对旧版本，建议再次核对全文衔接）':'未采用'),'复查：'+(x.feedback||'未复查'),'');});
    if(failures.length)lines.push('未完成：',...failures);
    const url=URL.createObjectURL(new Blob([lines.join('\n')],{type:'text/plain;charset=utf-8'}));const link=document.createElement('a');link.href=url;link.download='立项医生-会诊记录.txt';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
  });
})();
