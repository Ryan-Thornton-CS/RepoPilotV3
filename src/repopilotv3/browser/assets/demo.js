/** Deterministic, in-memory streaming fixture; no fetches, model calls or HTML rendering. */
'use strict';
const messages = document.getElementById('messages');
const input = document.getElementById('message');
let turn = 0, generation = 0, run = crypto.randomUUID();
/** Serialize versioned model requests with unique identities per reset. */
function request(tool, args) { return JSON.stringify({protocol_version:'1.0', request_id:`demo-${run}-${tool}`, tool, arguments:args}); }
/** Select a short scripted conversation without inspecting private tool bodies. */
function reply() {
  turn++;
  if (turn === 1) return 'I will register the demo task and checklist.\n```json\n' + request('task', {objective:'Exercise the local browser relay without changing source.',chat_name:'Browser relay demo'}) + '\n' + request('todo', {progress:{items:[{id:'inspect',text:'Search repository context',status:'active'}]}}) + '\n```';
  if (turn === 2) return 'I will try one bounded lookup.\n```json\n' + request('search', {query:'README'}) + '\n```';
  return 'Demo complete. The browser relay transferred requests and results. No source edits or commands were requested.';
}
/** Echo exact input and reveal the response in chunks with explicit completion state. */
async function submit(event) {
  event.preventDefault();
  if (!input.value.trim() || document.body.dataset.generating === 'true') return;
  const user = document.createElement('article'); user.className='user'; user.dataset.role='user'; user.textContent=input.value; messages.append(user); input.value='';
  const model=document.createElement('article'); model.className='assistant'; model.dataset.role='assistant'; model.dataset.complete='false'; messages.append(model);
  const token=++generation, text=reply(); document.body.dataset.generating='true'; document.getElementById('stop').hidden=false; document.getElementById('send').disabled=true; document.getElementById('state').textContent='Generating';
  for (let offset=0;offset<text.length;offset+=45) { await new Promise(resolve=>setTimeout(resolve,80)); if(token!==generation)return; model.textContent=text.slice(0,offset+45); messages.scrollTop=messages.scrollHeight; }
  model.dataset.complete='true'; finish();
}
/** Publish completion controls only after the full response is visible. */
function finish(){ document.body.dataset.generating='false';document.getElementById('stop').hidden=true;document.getElementById('send').disabled=false;document.getElementById('state').textContent='Ready'; }
document.getElementById('composer').addEventListener('submit',submit);
document.getElementById('stop').addEventListener('click',()=>{generation++;finish();document.getElementById('state').textContent='Stopped; response incomplete';});
document.getElementById('reset').addEventListener('click',()=>{generation++;turn=0;run=crypto.randomUUID();messages.replaceChildren();input.value='';finish();});
