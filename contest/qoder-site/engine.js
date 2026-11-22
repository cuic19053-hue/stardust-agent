/* Deterministic checks; unknown or ambiguous text stays pending. */
(function (root) {
  'use strict';
  const measured = /已实测|实测(?:达到|为|结果)|准确率(?:达到|为)|召回率(?:达到|为)|测试结果为/;
  const planned = /计划|拟开展|预计|目标|待测|将开展|尚待/;
  const noResult = /尚未开始|还未开始|未开始|没有实测|尚无实测|暂无实测|未开展|未完成测试|当前无实测/;
  const completed = /已完成测试|测试已完成|已实测|测试结果为/;
  const metric = /(准确率|召回率)\s*(?:达到|为|是|[:：])\s*(\d+(?:\.\d+)?)\s*%/g;
  function infer(text, kind) {
    if (kind === 'draft') {
      const a = measured.test(text), b = planned.test(text);
      return a === b ? 'unknown' : a ? 'measured' : 'planned';
    }
    const a = completed.test(text), b = noResult.test(text);
    return a === b ? 'unknown' : a ? 'measured' : 'none';
  }
  function metricPair(text) {
    const items = [...text.matchAll(metric)];
    if (items.length !== 1) return null;
    return {name: items[0][1], value: Number(items[0][2])};
  }
  function experimentId(text) {
    const ids = [...text.matchAll(/(?:实验|试验)\s*([A-Za-z0-9]+)(?![A-Za-z0-9])/g)].map(match => match[1].toLowerCase());
    return ids.length === 1 ? ids[0] : null;
  }
  function checkClaim(claim, record, draftChoice, recordChoice) {
    claim = claim.trim(); record = record.trim();
    if (!claim) return {status:'pending', message:'请先填写需要核对的表述。'};
    if (!record) return {status:'pending', message:'缺少对应的项目或实验记录，请补充依据。'};
    const claimExperiment = experimentId(claim), recordExperiment = experimentId(record);
    if (claimExperiment && recordExperiment && claimExperiment !== recordExperiment) return {status:'pending', message:'两段文字标注了不同实验，请先找到草稿对应实验的记录。'};
    const d = draftChoice === 'auto' ? infer(claim, 'draft') : draftChoice;
    const e = recordChoice === 'auto' ? infer(record, 'evidence') : recordChoice;
    if (d === 'unknown' || e === 'unknown') return {status:'pending', message:'无法确定两段话描述的是计划还是实测；请检查文字或手动选择状态。'};
    if (d === 'measured' && e === 'none') return {status:'conflict', message:'草稿描述为已实测，记录描述为尚无实测。请确认是否为同一实验，再修订表述。'};
    if (d === 'planned' && e === 'none') return {status:'ok', message:'计划与尚无实测的状态一致；这一项不验证其他事实。'};
    if (d === 'measured' && e === 'measured') {
      const a = metricPair(claim), b = metricPair(record);
      if (a && b && a.name === b.name) {
        if (a.value !== b.value) return {status:'conflict', message:'两段文字中同名指标的数字不同。请先确认是否来自同一次实验及相同计算方式。'};
        return {status:'pending', message:'同名指标数字相同，请人工确认是否为同一次实验、相同数据集与计算方式。'};
      }
      return {status:'pending', message:'两段均描述了实测，但指标或数字无法可靠对应，请人工核对。'};
    }
    return {status:'pending', message:'草稿与记录描述的阶段不同；请确认是否指向同一工作。'};
  }
  function cents(value) {
    const text = String(value).trim();
    if (!/^\d{1,10}(?:\.\d{1,2})?$/.test(text)) return null;
    const [whole, fraction = ''] = text.split('.');
    return Number(whole) * 100 + Number((fraction + '00').slice(0,2));
  }
  function checkBudget(total, early, late) {
    const values = [total, early, late].map(cents);
    if (values.some(v => v === null)) return {status:'pending', message:'请填写非负金额，最多保留两位小数。'};
    const [t, a, b] = values, delta = a + b - t;
    return {status:delta === 0 ? 'ok' : 'conflict', total:t, early:a, late:b, sum:a+b, delta,
      message:delta === 0 ? '前后期金额相加等于申请总额；不代表预算合理或已获批。' : '前后期金额之和与申请总额不一致，请核对计划。'};
  }
  function checkTeam(scope, count) {
    if (scope !== 'seu2026') return {status:'out', message:'尚未覆盖这个学校、年份或赛道，请查阅对应通知。'};
    if (!/^\d+$/.test(String(count)) || Number(count) < 1 || Number(count) > 99) return {status:'pending', message:'请填写 1 至 99 的整数学生人数。'};
    const n=Number(count);
    return {status:n > 5 ? 'conflict' : 'ok', message:n > 5 ? '当前填写 '+n+' 名学生，超过历史指南中团队不得超过 5 人的上限。' : '当前填写 '+n+' 名学生，未超过历史指南所述团队人数上限；其他资格仍需核对。'};
  }
  const engine={infer,checkClaim,checkBudget,checkTeam};
  if (typeof module !== 'undefined' && module.exports) module.exports=engine;
  root.ReviewEngine=engine;
})(typeof globalThis !== 'undefined' ? globalThis : this);
