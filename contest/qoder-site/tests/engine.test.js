'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const e = require('../engine.js');
const check=(draft,record,d='auto',r='auto')=>e.checkClaim(draft,record,d,r).status;
test('flags example and other percentages against an unstarted test',()=>{
  assert.equal(check('已实测，准确率达到 98.6%。','测试尚未开始。'),'conflict');
  assert.equal(check('本项目已实测，分类准确率达到 98.6%。','测试尚未开始，当前没有实测结果。'),'conflict');
  assert.equal(check('已实测，召回率达到 73%。','目前尚无实测。'),'conflict');
});
test('keeps missing and ambiguous evidence pending',()=>{
  assert.equal(check('准确率达到 98%',''),'pending');
  assert.equal(check('准确率 98%','结果不错'),'pending');
  assert.equal(check('计划测试，已实测准确率达到 98%','测试尚未开始'),'pending');
  assert.equal(check('已实测准确率达到 98%','测试尚未开始但测试已完成'),'pending');
  assert.equal(check('实验A已实测，准确率为90%。','实验B尚未开始，暂无实测。'),'pending');
});
test('recognizes plans, and allows user to correct ambiguous status',()=>{
  assert.equal(check('计划测试，准确率待测','测试尚未开始'),'ok');
  assert.equal(check('A','B','measured','none'),'conflict');
  assert.equal(check('A','','measured','none'),'pending');
});
test('metric disagreements are flagged but identical numbers need verification',()=>{
  assert.equal(check('已实测，准确率达到 88%','已实测，准确率为 89%'),'conflict');
  assert.equal(check('已实测，准确率达到 88%','已实测，准确率为 88%'),'pending');
  assert.equal(check('已实测，准确率达到 88%','已实测，召回率为 88%'),'pending');
});
test('budget calculates exact cents and rejects invalid values',()=>{
  assert.equal(e.checkBudget('1200','600','800').delta,20000);
  assert.equal(e.checkBudget('1200','600','750').delta,15000);
  assert.equal(e.checkBudget('1200','600','600').status,'ok');
  assert.equal(e.checkBudget('0.30','0.10','0.20').status,'ok');
  for (const value of ['', '-1', '1.001','1e3']) assert.equal(e.checkBudget('100',value,'1').status,'pending');
});
test('school rule applies only to selected batch and valid whole students',()=>{
  assert.equal(e.checkTeam('seu2026','6').status,'conflict');
  assert.equal(e.checkTeam('seu2026','5').status,'ok');
  for (const n of ['', '0', '1.5','100']) assert.equal(e.checkTeam('seu2026',n).status,'pending');
  assert.equal(e.checkTeam('other','6').status,'out');
});
