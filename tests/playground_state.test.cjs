// Execute the shipped browser controller with an in-memory DOM and fake fetch.
// This validates outgoing payload/cURL and turn identity, without model/network access.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
class Element {
  constructor(id = '') { this.id = id; this.value = ''; this.children = []; this.listeners = {}; this.dataset = {}; this._text = ''; this.hidden = false; }
  get textContent() { return this._text + this.children.map(node => node.textContent).join(''); }
  set textContent(value) { this._text = String(value); this.children = []; }
  addEventListener(name, callback) { (this.listeners[name] ||= []).push(callback); }
  async fire(name, event = {}) { for (const callback of this.listeners[name] || []) await callback(event); }
  replaceChildren(...nodes) { this.children = nodes; this._text = ''; }
  append(node) { this.children.push(node); }
  focus() {}
}
const names = ['playground-form','play-key','play-model','model-load-status','play-system','play-prompt','play-limit','play-temperature','play-reasoning','play-stream','play-curl','play-history','play-cost','play-status','prompt-count','play-stop','play-clear','play-output','play-echo','play-meta','play-send'];
const nodes = Object.fromEntries(names.map(name => [name, new Element(name)]));
const form = nodes['playground-form'];
form.dataset = {imageInput:'0', apiBase:'https://gateway.example/v1', defaultModel:'fixture-model'};
nodes['play-key'].value = '1'; nodes['play-model'].value = 'fixture-model';
nodes['play-limit'].value = '8'; nodes['play-temperature'].value = '0.2'; nodes['play-reasoning'].value = 'low';
nodes['play-stream'].checked = true;
let selected = 'chat.completions';
const radios = [new Element(), new Element()];
form.querySelector = () => ({value:selected});
form.querySelectorAll = selector => selector.startsWith('input[name=') ? radios : Object.values(nodes);
const document = {getElementById: id => nodes[id],querySelectorAll: () => [],createElement: () => new Element()};
const requests = [], receipts = [];
const reply = (json, headers = {}) => ({ok:true, headers:{get:name => headers[name]},json:async() => json});
const fetch = async (url, options) => {
  if (url.startsWith('/developer/playground/models')) return reply({data:[{id:'fixture-model'}]});
  if (url.startsWith('/api/developer/requests?')) {
    const requestId = new URL(url,'https://gateway.example').searchParams.get('request_id');
    receipts.push(requestId);
    const id = Number(requestId.split('/')[1]);
    return reply({id,request_id:requestId,billing_status:'settled',charged_credits:'0.1',reserved_credits:'1',released_credits:'1'});
  }
  assert.equal(url,'/developer/playground/run');
  const request = JSON.parse(options.body); requests.push(request);
  const id = requests.length, output = `answer-${id}`;
  const data = request.endpoint === 'responses'
    ? {output_text:output,usage:{input_tokens:2,output_tokens:1}}
    : {choices:[{message:{content:output}}],usage:{prompt_tokens:2,completion_tokens:1}};
  const response = reply(data,{'x-request-id':`order/${id}`});
  if (request.payload.stream) {
    let sent = false;
    const text = 'data: '+JSON.stringify({choices:[{delta:{content:output},finish_reason:'stop'}],usage:data.usage})+'\n\ndata: [DONE]\n\n';
    response.body = {getReader:() => ({read:async() => sent ? {done:true} : (sent = true,{value:new TextEncoder().encode(text),done:false}),cancel:async() => {}})};
  }
  return response;
};
vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../app/web/static/ai.js'),'utf8'), {
  document,window:{addEventListener(){}},fetch,performance,AbortController,AbortSignal,
  URLSearchParams,TextDecoder,setTimeout,clearTimeout,
});
function curlPayload() {
  const quoted = nodes['play-curl'].textContent.split('  -d ')[1];
  return JSON.parse(quoted.slice(1,-1).replaceAll("'\\''", "'"));
}
async function submit(prompt) {
  nodes['play-prompt'].value = prompt;
  await form.fire('input');
  await form.fire('submit',{preventDefault(){}});
  assert.equal(nodes['play-prompt'].value,'');
  assert.deepEqual(curlPayload(),requests.at(-1).payload);
  assert.match(nodes['play-curl'].textContent,/Bearer YOUR_API_KEY/);
}
(async () => {
  await new Promise(setImmediate);
  await submit("first user's prompt");
  assert.equal(requests[0].payload.stream,true);
  assert.equal(requests[0].payload.messages.length,1);
  assert.equal(nodes['play-history'].hidden,true);
  assert.match(nodes['play-cost'].textContent,/已结算/);
  assert.doesNotMatch(nodes['play-cost'].textContent,/settled/);
  nodes['play-prompt'].value = 'second prompt'; await form.fire('input');
  assert.equal(curlPayload().messages.length,3);
  await submit('second prompt');
  assert.match(nodes['play-history'].textContent,/answer-1/);
  assert.doesNotMatch(nodes['play-history'].textContent,/answer-2/);
  assert.match(nodes['play-history'].textContent,/order\/1/);
  assert.equal(nodes['play-history'].children[0].children.at(-1).href,'/developer/usage/requests/1');
  assert.equal(nodes['play-output'].textContent,'answer-2');
  selected = 'responses'; await radios[1].fire('change');
  nodes['play-stream'].checked = false;
  await submit('response prompt');
  assert.equal(requests[2].endpoint,'responses');
  assert.equal(requests[2].payload.input.length,1);
  assert.deepEqual(requests[2].payload.reasoning,{effort:'low'});
  assert.equal(requests[2].payload.stream,false);
  nodes['play-stream'].checked = true; await form.fire('input');
  assert.equal(curlPayload().stream,false); // Empty composer still shows the last real request.
  assert.match(nodes['play-curl'].textContent,/\/v1\/responses/);
  assert.deepEqual(receipts,['order/1','order/2','order/3']);
  console.log('playground SSE/JSON, exact submitted cURL, next-turn preview, transcript and per-turn receipts: OK');
})().catch(error => { console.error(error); process.exitCode = 1; });
