'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const {splitDoc, buildRoleSample, reviewerIndex} = require('../doctor.js');

test('splitDoc keeps full text in bounded line-aligned chunks', () => {
  const doc = ('段落内容示例文本。\n').repeat(200);
  const chunks = splitDoc(doc);
  assert.equal(chunks.join(''), doc);
  assert.ok(chunks.every(c => c.length <= 2000));
  assert.ok(chunks.slice(0, -1).every(c => c.endsWith('\n')));
});

test('buildRoleSample returns short material unchanged', () => {
  const s = '一、研究目的\n一段短材料。';
  assert.equal(buildRoleSample(s), s);
});

test('buildRoleSample keeps verbatim head, tail and chapter excerpts for long material', () => {
  const marks = '一二三四五六七八';
  const doc = Array.from({length: 8}, (_, i) => `${marks[i]}、第${i + 1}章\n` + `内容${i}说明。\n`.repeat(400)).join('');
  const sample = buildRoleSample(doc);
  assert.ok(sample.length <= 3600);
  assert.ok(sample.startsWith(doc.slice(0, 600)));
  assert.ok(sample.endsWith(doc.slice(-600)));
  assert.ok(sample.includes('三、第3章'));
  for (const line of sample.split('\n')) if (line.trim()) assert.ok(doc.includes(line));
});

test('buildRoleSample falls back to a middle excerpt when no headings exist', () => {
  const doc = '无标题的连续材料。'.repeat(600);
  const sample = buildRoleSample(doc);
  assert.ok(sample.length <= 3600);
  assert.ok(sample.startsWith(doc.slice(0, 600)));
  assert.ok(doc.includes(sample.split('\n')[1]));
});

test('reviewerIndex never picks the author and rotates across findings', () => {
  assert.equal(reviewerIndex(1, 0, 0), -1);
  assert.equal(reviewerIndex(0, 0, 0), -1);
  for (let author = 0; author < 3; author++) for (let f = 0; f < 9; f++) {
    const j = reviewerIndex(3, author, f);
    assert.notEqual(j, author);
    assert.ok(j >= 0 && j < 3);
  }
  assert.deepEqual(new Set([0, 1, 2].map(f => reviewerIndex(3, 0, f))), new Set([1, 2]));
  assert.equal(reviewerIndex(2, 1, 5), 0);
  assert.equal(reviewerIndex(2, 0, 5), 1);
});
