const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

// Load this copy's inline script. Replace only startup with test access to its closures.
function setup() {
  const html = fs.readFileSync(path.join(__dirname, '../webui/index.html'), 'utf8');
  const script = [...html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/gi)].map(m => m[1]).find(s => s.includes('function pollJob('));
  const timers = new Map(), requests = [], elements = new Map();
  let now = 0, serial = 0;
  function element(key) {
    if (!elements.has(key)) elements.set(key, {style:{}, classList:{add(){}, remove(){}, toggle(){}}, addEventListener(){}, querySelectorAll(){return [];}});
    return elements.get(key);
  }
  const context = {
    document:{querySelector:element, querySelectorAll(){return [];}, addEventListener(){}},
    maibot:{query(name,args){return new Promise((resolve,reject) => requests.push({name,args,resolve,reject}));}},
    setTimeout(fn, ms){const id = ++serial; timers.set(id,{fn,time:now+ms}); return id;},
    clearTimeout(id){timers.delete(id);}, console
  };
  context.window = context;
  vm.runInNewContext(script.replace(/\binit\(\);\s*\}\)\(\);\s*$/, 'window.api = {pollJob, loadStudioStatus, paintHealth, readState, S}; })();'), context);
  assert.ok(context.api);
  async function flush(){await new Promise(resolve => setImmediate(resolve));}
  async function advance(ms){
    const end = now + ms;
    for(;;){
      const entry = [...timers].filter(([,t]) => t.time <= end).sort((a,b) => a[1].time-b[1].time)[0];
      if(!entry) break;
      timers.delete(entry[0]); now = entry[1].time; entry[1].fn(); await flush();
    }
    now = end;
  }
  return {api:context.api, element, requests, advance, flush};
}

test('job switch waits for the old RPC and drops its late terminal result', async () => {
  const {api, requests, advance, flush, element} = setup();
  api.pollJob('old'); await advance(2200);
  api.pollJob('new'); await advance(60000);
  assert.equal(requests.length, 1);
  requests[0].resolve({status:'done',meta:{title:'obsolete'}}); await flush();
  assert.notEqual(element('#playTitle').textContent, 'obsolete');
  await advance(2199); assert.equal(requests.length, 1);
  await advance(1); assert.equal(requests[1].args.job_id, 'new');
  requests[1].resolve({status:'cancelled'}); await flush(); await advance(60000);
  assert.equal(requests.filter(r => r.name === 'job').length, 2);
  assert.equal(element('#monBadge').textContent, 'CANCELLED');
});

test('job deadline expires while the original RPC remains in flight', async () => {
  const {api, requests, advance, flush, element} = setup();
  api.pollJob('stuck'); await advance(621 * 2200);
  assert.equal(requests.length, 1);
  assert.match(element('#playStatus').textContent, /等待超时/);
  requests[0].resolve({status:'done',meta:{title:'too late'}}); await flush(); await advance(60000);
  assert.equal(requests.length, 1); assert.notEqual(element('#playTitle').textContent, 'too late');
});

test('studio timeout retains the RPC slot and coalesces refreshes', async () => {
  const {api, requests, advance, flush, element} = setup();
  api.loadStudioStatus(true); await advance(0);
  requests[0].resolve({ok:true,task:{status:'running'}}); await flush();
  await advance(4000); await advance(6500);
  assert.equal(element('#stMsg').textContent, '请求超时');
  api.loadStudioStatus(true); api.loadStudioStatus(false); await advance(20000);
  assert.equal(requests.length, 2);
  requests[1].resolve({ok:true,task:{status:'done'}}); await flush();
  assert.equal(element('#stBadge').textContent, 'OFFLINE');
  await advance(0); assert.equal(requests.length, 3);
  requests[2].resolve({ok:true,task:{status:'idle'}}); await flush(); await advance(20000);
  assert.equal(requests.length, 3); assert.equal(element('#stBadge').textContent, 'IDLE');
});

test('health cadence follows the latest status and re-arms after rejection', async () => {
  const {api, requests, advance, flush, element} = setup();
  api.paintHealth({ok:false,starting:true}); await advance(2200);
  api.readState(); api.readState();
  api.paintHealth({ok:false,message:'newer'}); await advance(60000);
  assert.equal(requests.length, 1);
  requests[0].resolve({health:{ok:true,message:'old'}}); await flush();
  assert.equal(element('#healthText').textContent, 'newer');
  await advance(5999); assert.equal(requests.length, 1);
  await advance(1); requests[1].reject(new Error('offline')); await flush();
  await advance(6000); assert.equal(requests.length, 3);
});
