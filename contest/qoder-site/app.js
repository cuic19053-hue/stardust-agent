'use strict';
const $ = id => document.getElementById(id);
const draft = $('draft'), evidence = $('evidence');
const guideUrl = 'https://jwc.seu.edu.cn/_upload/article/files/71/75/58681d784ea4a3430f796a7d7003/f2be4fc1-b099-4458-adb5-f546070b6839.pdf';
const noticeUrl = 'https://jwc.seu.edu.cn/_upload/article/files/71/75/58681d784ea4a3430f796a7d7003/f9da8747-fcdb-4048-8a57-8ba5a35cfdfc.pdf';
const labels = {conflict:'有冲突',pending:'待核',ok:'已对上',out:'不适用'};
let currentChecks = [];
let modelFindings = null, modelRevision = 0, modelRunning = false, modelAvailable = false;
const aiLabels = {potential_conflict:'可能不一致',needs_evidence:'依据待核',consistent:'可能一致'};
function escapeHtml(value) { return String(value).replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch])); }
function money(cents) { return (cents / 100).toLocaleString('zh-CN',{maximumFractionDigits:2}); }
function card(item) {
  const quote = (item.left || item.right) ? '<div class="quote-pair">'+(item.left ? '<div class="quote-box"><b>当前填写</b>'+escapeHtml(item.left)+'</div>' : '')+(item.right ? '<div class="quote-box"><b>对照依据</b>'+escapeHtml(item.right)+'</div>' : '')+'</div>' : '';
  const source = item.url ? '<a href="'+item.url+'" target="_blank" rel="noopener noreferrer">查看学校原文 ↗</a>' : '';
  const jump = item.target ? '<button type="button" class="finding-jump" data-target="'+item.target+'">返回修改 ↗</button>' : '';
  return '<article class="finding"><div class="finding-top"><span class="finding-title">'+escapeHtml(item.title)+'</span><span class="status status-'+item.status+'">'+labels[item.status]+'</span></div><p>'+escapeHtml(item.message)+'</p>'+quote+'<div class="finding-actions">'+jump+source+'</div></article>';
}
function update() {
  $('charCount').textContent = draft.value.length+' 字';
  const claim = ReviewEngine.checkClaim(draft.value,evidence.value,$('draftState').value,$('evidenceState').value);
  const budget = ReviewEngine.checkBudget($('budgetTotal').value,$('budgetEarly').value,$('budgetLate').value);
  const team = ReviewEngine.checkTeam($('scope').value,$('studentCount').value);
  const inScope = $('scope').value === 'seu2026';
  const calc = $('budgetResult');
  calc.className='calc-result '+budget.status;
  if (budget.status === 'pending') calc.innerHTML='<span>待核 · 金额无效或缺失</span><strong>—</strong>';
  else {
    const amount=budget.delta===0?'金额已对上':budget.delta>0?'超出 '+money(budget.delta)+' 元':'还差 '+money(-budget.delta)+' 元';
    calc.innerHTML='<span>'+amount+'</span><strong>'+money(budget.early)+' + '+money(budget.late)+' = '+money(budget.sum)+'</strong>';
  }
  $('ruleResult').className='rule-result '+team.status;
  $('ruleResult').textContent=team.message;
  currentChecks=[
    {...claim,title:'成果与项目记录',left:draft.value.trim(),right:evidence.value.trim(),target:'draft'},
    {...budget,title:'预算前后期对账',left:budget.status==='pending'?'':money(budget.total)+' 元',right:budget.status==='pending'?'':money(budget.early)+' + '+money(budget.late)+' = '+money(budget.sum)+' 元',target:'budgetTotal'},
    {...team,title:'团队人数规则',left:inScope && $('studentCount').value ? $('studentCount').value+' 名学生':'',right:inScope?'历史指南第 1 页：团队不得超过 5 人。':'',url:inScope?guideUrl:'',target:'studentCount'},
    inScope ? {title:'申报资格证明',status:'pending',message:'历史批次还有其他申报资格要求；本页面没有核验相关证明，请查阅通知并自行确认。',right:'学校通知第 2 页：申报资格要求。',url:noticeUrl} : {title:'申报资格证明',status:'out',message:'当前范围尚未覆盖，请查阅对应学校和批次的通知。'}
  ];
  $('findings').innerHTML=currentChecks.map(card).join('');
  const counts=currentChecks.reduce((a,x)=>(a[x.status]=(a[x.status]||0)+1,a),{});
  $('summaryCounts').innerHTML=Object.keys(labels).map(s=>'<span class="count-pill"><strong>'+(counts[s]||0)+'</strong>'+labels[s]+'</span>').join('');
  const issue=(counts.conflict||0)+(counts.pending||0), hasOut=(counts.out||0)>0;
  $('resultHeadline').textContent=counts.conflict?'先看需要核对的冲突':issue?'还有信息需要确认':hasOut?'部分范围尚未覆盖':'当前检查项已对上';
  $('resultMark').textContent=counts.conflict?'!':issue||hasOut?'?':'✓';
  $('resultMark').style.background=counts.conflict?'#f4d9cd':issue||hasOut?'#f4e8c9':'#d8e9dc';
  $('resultMark').style.color=counts.conflict?'#9a432d':issue||hasOut?'#90671d':'#38684b';
}
function resetStates() { $('draftState').value='auto'; $('evidenceState').value='auto'; }
$('findings').addEventListener('click', event=>{
  const button=event.target.closest('[data-target]');
  if (button) { const field=$(button.dataset.target); field.scrollIntoView({behavior:'smooth',block:'center'}); field.focus({preventScroll:true}); }
});
$('runCheck').addEventListener('click',()=>{ update(); $('results').scrollIntoView({behavior:'smooth',block:'start'}); });
$('loadDemo').addEventListener('click',()=>{
  invalidateModel(); draft.value='本项目已实测，分类准确率达到 98.6%。'; evidence.value='测试尚未开始，当前没有实测结果。';
  $('budgetTotal').value='1200'; $('budgetEarly').value='600'; $('budgetLate').value='800'; $('studentCount').value='6'; $('scope').value='seu2026'; resetStates();
  $('modeLabel').textContent='模拟案例 · 虚构项目记录'; update(); draft.focus();
});
$('useMyText').addEventListener('click',()=>{
  invalidateModel(); draft.value=''; evidence.value=''; $('budgetTotal').value=''; $('budgetEarly').value=''; $('budgetLate').value=''; $('studentCount').value=''; $('scope').value='other'; resetStates();
  $('modeLabel').textContent='自填材料 · 仅在此页面核对'; update(); draft.focus();
});
draft.addEventListener('input',()=>{ $('draftState').value='auto'; invalidateModel(); update(); });
evidence.addEventListener('input',()=>{ $('evidenceState').value='auto'; invalidateModel(); update(); });
['draftState','evidenceState','budgetTotal','budgetEarly','budgetLate','studentCount','scope'].forEach(id=>$(id).addEventListener('input',update));
$('downloadReport').addEventListener('click',()=>{
  update();
  const rows=['立项医生 · 本次核对记录','生成时间：'+new Date().toLocaleString('zh-CN'),'适用范围：'+$('scope').selectedOptions[0].textContent,'草稿状态：'+$('draftState').selectedOptions[0].textContent,'记录状态：'+$('evidenceState').selectedOptions[0].textContent,'说明：记录包含当前填写内容；结果仅供人工复核，不代表学校审核结论。',''];
  currentChecks.forEach((item,i)=>{ rows.push((i+1)+'. '+item.title+' ['+labels[item.status]+']',item.message); if(item.left) rows.push('当前填写：'+item.left); if(item.right) rows.push('对照依据：'+item.right); if(item.url) rows.push('来源：'+item.url); rows.push(''); });
  if (modelFindings) { rows.push('Qwen 补充核对（仅供人工复核）','模型：'+modelFindings.model); modelFindings.findings.forEach((item,i)=>{rows.push((i+1)+'. ['+aiLabels[item.status]+'] '+item.explanation,'草稿原文：'+item.draft_quote,'记录原文：'+(item.evidence_quote||'未提供'),'建议：'+item.next_step,'');}); }
  const url=URL.createObjectURL(new Blob([rows.join('\n')],{type:'text/plain;charset=utf-8'}));
  const link=document.createElement('a'); link.href=url; link.download='立项医生-核对记录.txt'; link.click(); setTimeout(()=>URL.revokeObjectURL(url),1000);
});
update();

function invalidateModel() {
  modelRevision++;
  modelFindings=null;
  $('modelFindings').innerHTML='';
  if (modelRunning) $('modelMessage').textContent='材料已修改，本次模型结果返回后将丢弃。';
  else if (modelAvailable) $('modelMessage').textContent='材料已修改，点击按钮重新核对。';
}
function renderModel(data) {
  $('modelFindings').innerHTML=data.findings.map(item=>'<article class="finding"><div class="finding-top"><span class="finding-title">Qwen 补充意见</span><span class="status status-pending">'+escapeHtml(aiLabels[item.status])+'</span></div><p>'+escapeHtml(item.explanation)+'</p><div class="quote-pair"><div class="quote-box"><b>草稿原文</b>'+escapeHtml(item.draft_quote)+'</div><div class="quote-box"><b>记录原文</b>'+escapeHtml(item.evidence_quote||'未提供')+'</div></div><p>下一步：'+escapeHtml(item.next_step)+'</p></article>').join('');
}
async function checkModelAvailability() {
  if (location.protocol === 'file:') { $('modelStatus').textContent='请启动本地服务'; $('modelMessage').textContent='请按项目说明启动本地服务，再通过本机地址打开页面。'; return; }
  try {
    const response=await fetch('/api/model-status',{cache:'no-store'});
    if (!response.ok) throw new Error('unavailable');
    const data=await response.json(); modelAvailable=data.available === true;
    $('modelStatus').textContent=modelAvailable?data.model+' · 本机':'模型未就绪';
    $('modelMessage').textContent=modelAvailable?'模型仅在你点击时运行，首次加载可能需要等待。':'请启动 Ollama 并准备本地模型，规则核对仍可使用。';
    $('runModel').disabled=!modelAvailable || modelRunning;
  } catch { $('modelStatus').textContent='服务未连接'; $('modelMessage').textContent='请按项目说明启动本地服务；规则核对仍可使用。'; }
}
$('runModel').addEventListener('click',async()=>{
  if (modelRunning || !modelAvailable) return;
  if (!draft.value.trim()) { $('modelMessage').textContent='请先填写需要核对的草稿。'; draft.focus(); return; }
  if (draft.value.length>4000 || evidence.value.length>4000) { $('modelMessage').textContent='每段最多 4000 字，请缩短后重试。'; return; }
  const revision=modelRevision, input={draft:draft.value,evidence:evidence.value};
  modelRunning=true; $('runModel').disabled=true; $('modelMessage').textContent='本机模型正在核对，首次加载可能较慢…'; $('modelFindings').innerHTML=''; modelFindings=null;
  try {
    const response=await fetch('/api/review',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(input)});
    let data=await response.json();
    if (!response.ok) throw new Error(typeof data.detail==='string'?data.detail:'模型暂不可用，请稍后重试。');
    if (revision!==modelRevision) { $('modelMessage').textContent='材料已修改，旧结果已丢弃；请重新核对。'; return; }
    modelFindings=data; renderModel(data); $('modelMessage').textContent=data.notice;
  } catch (error) { $('modelMessage').textContent=revision===modelRevision ? (error.message || '无法连接本地模型，请检查服务。') : '材料已修改，本次请求未生成可用结果；请重新核对。'; }
  finally { modelRunning=false; $('runModel').disabled=!modelAvailable; }
});
checkModelAvailability();
