const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(process.env.FEIYU_UPLOAD_UI || path.join(__dirname, '../webui/index.html'), 'utf8');
const id = 'ab'.repeat(16);
function region(first, last) {
  const start = html.indexOf(first), end = html.indexOf(last, start + first.length);
  assert.ok(start >= 0 && end > start);
  return html.slice(start, end);
}
function setup(override = () => undefined) {
  const calls = [], notices = [], nodes = new Map();
  const $ = key => {
    if (!nodes.has(key)) nodes.set(key, {value:'', style:{}, events:{}, addEventListener(k, cb){this.events[k] = cb;}, pause(){}, removeAttribute(){}, focus(){}});
    return nodes.get(key);
  };
  const S = {selectedTrack:{title:'old'}, draft:{}, previewSeconds:60};
  const ctx = vm.createContext({$, $$:() => [], S, btoa, setTimeout:queueMicrotask, toast:msg => notices.push(msg), setMonitorProgress(){}, setRenderButtonBusy(){}, pollJob(){}, async rpcAction(name, args) {
    calls.push({name, ...args});
    const reply = override(args);
    if (reply !== undefined) return reply;
    if (name === 'preview') return {ok:true, job_id:'job'};
    if (args.op === 'start') return {ok:true, token:'token', chunk_size:24576};
    if (args.op === 'finish') return {ok:true, status:'uploading'};
    if (args.op === 'status') return {ok:true, status:'done', upload_id:id};
    return {ok:true};
  }});
  vm.runInContext(region('  let localSource = null;', '  function doSearch()') + region('  function doRenderOrCancel()', '  // ---- Recent Jobs'), ctx);
  return {ctx, S, calls, notices, $};
}
function file() {
  const blob = new Blob([Buffer.alloc(24577, 255)]);
  blob.name = '我的歌.wav';
  return blob;
}
test('native input and chunk transfer, then reuse in both render modes and clear', async () => {
  assert.match(html, /<input\b[^>]*id="sourceFile"[^>]*type="file"/);
  const {ctx,S,calls,$} = setup();
  await ctx.uploadSource(file());
  assert.equal(S.selectedTrack, null);
  assert.match($('#sourceFileName').textContent, /我的歌\.wav.*已就绪/);
  assert.deepEqual(calls.map(c => c.op), ['start','chunk','chunk','finish','status']);
  const chunks = calls.filter(c => c.op === 'chunk');
  assert.deepEqual(chunks.map(c => c.index), [0,1]);
  assert.deepEqual(chunks.map(c => Buffer.from(c.data,'base64').length), [24576,1]);
  assert.ok(chunks.every(c => JSON.stringify(c).length < 65536));
  $('#song').value = '另一个歌名';
  for(const seconds of [60,0]) {
    S.previewSeconds = seconds;
    ctx.doRenderOrCancel();
    const req = JSON.parse(calls.at(-1).payload);
    assert.equal(req.preview_seconds, seconds);
    assert.equal(req.upload_id, id);
    assert.equal(req.song, '我的歌.wav');
    assert.equal(req.selected_track, undefined);
  }
  $('#btnClearSource').events.click();
  ctx.doRenderOrCancel();
  const fallback = JSON.parse(calls.at(-1).payload);
  assert.equal(fallback.upload_id, undefined);
  assert.equal(fallback.song, '另一个歌名');
});
test('late start after cancellation cleans token and never uploads', async () => {
  let release;
  const {ctx,calls,$} = setup(args => args.op === 'start' ? new Promise(resolve => {release=resolve;}) : undefined);
  const pending = ctx.uploadSource(file());
  ctx.doRenderOrCancel();
  $('#btnCancelSource').events.click();
  release({ok:true,token:'late'});
  await pending;
  assert.deepEqual(calls.map(c => c.op), ['start','cancel']);
  assert.equal(calls.at(-1).token, 'late');
  assert.equal($('#sourceFileName').textContent, '未选择音源');
});
test('invalid input makes no RPC, explicit track choice replaces completed upload', async () => {
  const {ctx,calls} = setup();
  for(const input of [{name:'big.mp3', size:32*1024*1024+1}, {name:'empty.wav',size:0}, {name:'wrong.exe',size:1}]) await ctx.uploadSource(input);
  assert.equal(calls.length, 0);
  await ctx.uploadSource(file());
  ctx.selectTrack({title:'新音源'});
  ctx.doRenderOrCancel();
  const req = JSON.parse(calls.at(-1).payload);
  assert.equal(req.selected_track.title, '新音源');
  assert.equal(req.upload_id, undefined);
});
