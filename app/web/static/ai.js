(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  function updateExample() {
    if (!$('config-code')) return;
    const base = $('ai-base').textContent;
    const appName = $('config-code').dataset.appName;
    const model = $('config-model').value.trim() || $('config-model').defaultValue;
    const quoted = JSON.stringify(model);
    const payload = JSON.stringify({model, messages: [{role: 'user', content: '你好'}], stream: false}, null, 2);
    const shellQuote = value => "'" + value.replaceAll("'", "'\\''") + "'";
    const examples = {
      curl: `# 先设置环境变量 LUOMO_API_KEY\ncurl ${base}/chat/completions \\\n  -H "Authorization: Bearer $LUOMO_API_KEY" \\\n  -H 'Content-Type: application/json' \\\n  -d ${shellQuote(payload)}`,
      responses: `curl ${base}/responses \\\n  -H "Authorization: Bearer $LUOMO_API_KEY" \\\n  -H 'Content-Type: application/json' \\\n  -d ${shellQuote(JSON.stringify({model, input: '你好', stream: false}, null, 2))}`,
      python: `# pip install openai\nimport os\nfrom openai import OpenAI\n\nclient = OpenAI(\n    base_url="${base}",\n    api_key=os.environ["LUOMO_API_KEY"],\n)\nresponse = client.chat.completions.create(\n    model=${quoted},\n    messages=[{"role": "user", "content": "你好"}],\n)\nprint(response.choices[0].message.content)`,
      javascript: `// npm install openai · 在服务端运行\nimport OpenAI from 'openai';\nconst client = new OpenAI({\n  baseURL: '${base}',\n  apiKey: process.env.LUOMO_API_KEY,\n});\nconst response = await client.chat.completions.create({\n  model: ${quoted},\n  messages: [{ role: 'user', content: '你好' }],\n});\nconsole.log(response.choices[0].message.content);`,
      airi: `提供商：OpenAI Compatible / OpenAI 兼容\nBase URL：${base}\nAPI Key：填写你自己的 ${appName} 密钥\n模型：${model}\n协议：Chat Completions\n\n需要权限：models:read, chat:completions\n模型白名单：${model}\n\n先保存提供商配置，再在聊天模块选择该提供商和模型。\n若测试报错，请检查 /v1 是否重复，以及密钥的权限与有效期。`
    };
    $('config-code').textContent = examples[$('config-client').value];
  }
  $('config-model')?.addEventListener('input', updateExample);
  $('config-client')?.addEventListener('change', updateExample);
  document.querySelectorAll('[data-select-model]').forEach(button => button.addEventListener('click', () => {
    $('config-model').value = button.dataset.selectModel; updateExample();
    $('configuration').scrollIntoView({behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth'});
    $('config-model').focus({preventScroll: true});
  }));
  updateExample();
  if (!$('playground-form')) return;
  const form = $('playground-form');
  const imageInput = form.dataset.imageInput === '1';
  let controller;
  let models = [];
  const endpoint = () => form.querySelector('input[name="play-endpoint"]:checked').value;
  const setStatus = text => { $('play-status').textContent = text; };
  const apiError = async response => {
    const data = await response.json().catch(() => ({}));
    const hints = {401: '登录已失效，请刷新页面重新登录。', 402: '积分不足，请先到积分页兑换或充值。', 403: '这把密钥没有该模型或接口的权限。', 429: '请求太频繁或已达配额，请稍后再试。'};
    return `HTTP ${response.status} · ${data.error?.message || hints[response.status] || '请求失败，请检查密钥、权限和服务状态。'}`;
  };

  async function loadModels() {
    const key = $('play-key').value;
    const select = $('play-model');
    const previous = select.value || form.dataset.defaultModel;
    $('model-load-status').textContent = '正在读取这把密钥可用的模型…';
    select.disabled = true;
    try {
      const response = await fetch(`/developer/playground/models?key_id=${encodeURIComponent(key)}`, {credentials: 'same-origin', cache: 'no-store', signal: AbortSignal.timeout(15000)});
      if (!response.ok) throw new Error(await apiError(response));
      models = (await response.json()).data || [];
      if (!models.length) { $('model-load-status').textContent = '没有读到可用模型：上游目录暂不可达，或这把密钥没有模型权限。仍可用默认模型直接发送。'; return; }
      select.replaceChildren(...models.map(model => {
        const option = document.createElement('option');
        option.value = model.id;
        option.textContent = `${model.id}${model.vision ? ' · 支持图像' : ''}${model.category ? ' — ' + model.category : ''}`;
        return option;
      }));
      select.value = models.some(model => model.id === previous) ? previous : (models.some(model => model.id === form.dataset.defaultModel) ? form.dataset.defaultModel : models[0].id);
      $('model-load-status').textContent = `这把密钥可用 ${models.length} 个模型。`;
    } catch (error) {
      $('model-load-status').textContent = error.name === 'TimeoutError' ? '读取模型超时，可直接发送，或稍后切换密钥重试。' : error.message;
    } finally { select.disabled = false; }
  }

  function buildPayload(streamed) {
    const model = $('play-model').value.trim();
    const system = ($('play-system')?.value || '').trim();
    const prompt = $('play-prompt').value;
    const image = imageInput ? ($('play-image')?.value || '').trim() : '';
    if (endpoint() === 'responses') {
      const content = image ? [{type: 'input_text', text: prompt}, {type: 'input_image', image_url: image}] : prompt;
      const payload = {model, input: [{role: 'user', content}], stream: streamed};
      if (system) payload.instructions = system;
      return payload;
    }
    const content = image ? [{type: 'text', text: prompt}, {type: 'image_url', image_url: {url: image}}] : prompt;
    const messages = [...(system ? [{role: 'system', content: system}] : []), {role: 'user', content}];
    return {model, messages, stream: streamed, ...(streamed ? {stream_options: {include_usage: true}} : {})};
  }

  // Normalise both protocols to {text, usage, done} per server-sent event.
  function readEvent(data, kind) {
    if (data.error) throw new Error(data.error.message || '流式生成失败');
    if (kind === 'responses') {
      if (data.type === 'response.output_text.delta') return {text: data.delta || ''};
      if (data.type === 'response.completed') return {usage: data.response?.usage, done: true};
      if (data.type === 'response.failed' || data.type === 'response.incomplete') throw new Error(data.response?.error?.message || '上游未完成这次生成');
      return {};
    }
    return {text: data.choices?.[0]?.delta?.content || '', usage: data.usage || undefined, finished: data.choices?.some(choice => choice.finish_reason)};
  }
  function responsesText(data) {
    if (typeof data.output_text === 'string') return data.output_text;
    return (data.output || []).flatMap(item => item.content || []).filter(part => part.type === 'output_text').map(part => part.text).join('');
  }

  $('play-key').addEventListener('change', loadModels);
  $('play-prompt').addEventListener('input', () => { $('prompt-count').textContent = `${$('play-prompt').value.length} / 8000`; });
  $('play-prompt').addEventListener('keydown', event => { if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) { event.preventDefault(); form.requestSubmit(); } });
  $('play-stop').addEventListener('click', () => controller?.abort());
  $('play-clear').addEventListener('click', () => {
    controller?.abort(); $('play-prompt').value = ''; $('prompt-count').textContent = '0 / 8000';
    $('play-output').textContent = ''; $('play-echo').hidden = true; $('play-meta').textContent = '—'; setStatus('已清空');
    $('play-prompt').focus();
  });
  window.addEventListener('pagehide', () => controller?.abort());

  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (controller) return;
    controller = new AbortController();
    const timeout = setTimeout(() => controller?.abort(), 180000);
    const start = performance.now();
    const kind = endpoint();
    const streamed = $('play-stream').checked;
    $('play-send').disabled = true; $('play-stop').disabled = false;
    $('play-output').textContent = ''; $('play-meta').textContent = '…';
    $('play-echo').textContent = $('play-prompt').value; $('play-echo').hidden = false;
    setStatus('正在连接…');
    let usage, requestId = '', output = '', firstByte;
    try {
      const response = await fetch('/developer/playground/run', {
        method: 'POST', credentials: 'same-origin', cache: 'no-store', signal: controller.signal,
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({key_id: Number($('play-key').value), endpoint: kind, payload: buildPayload(streamed)})
      });
      requestId = response.headers.get('x-request-id') || '';
      if (!response.ok) throw new Error(await apiError(response));
      if (!streamed) {
        const data = await response.json();
        if (data.error) throw new Error(data.error.message || '上游生成失败');
        output = kind === 'responses' ? responsesText(data) : (data.choices?.[0]?.message?.content || '');
        $('play-output').textContent = output || '（模型没有返回文本）';
        usage = data.usage;
      } else {
        const reader = response.body.getReader();
        const decoder = new TextDecoder(); let pending = '', done = false, finished = false;
        const consume = eventText => {
          const raw = eventText.split(/\r?\n/).filter(line => line.startsWith('data:')).map(line => line.slice(5).trimStart()).join('\n');
          if (!raw) return;
          if (raw === '[DONE]') { done = true; return; }
          const step = readEvent(JSON.parse(raw), kind);
          if (step.usage) usage = step.usage;
          if (step.done) done = true;
          if (step.finished) finished = true;
          if (step.text) {
            if (firstByte === undefined) firstByte = performance.now() - start;
            output += step.text; $('play-output').textContent = output; setStatus('正在生成…');
          }
        };
        try {
          while (true) {
            const {value, done: closed} = await reader.read();
            pending += decoder.decode(value, {stream: !closed});
            let boundary;
            while ((boundary = /\r?\n\r?\n/.exec(pending))) { const chunk = pending.slice(0, boundary.index); pending = pending.slice(boundary.index + boundary[0].length); consume(chunk); }
            if (closed) { if (pending.trim()) consume(pending); break; }
          }
          if (!done) throw new Error(finished ? '收到结束标记，但连接未完整结束，用量可能缺失' : '连接提前结束，回答可能不完整');
        } finally { await reader.cancel().catch(() => {}); }
      }
      setStatus('完成');
    } catch (error) {
      setStatus(error.name === 'AbortError' ? '已停止' : '失败');
      if (error.name !== 'AbortError') $('play-output').textContent = (output ? output + '\n\n' : '') + '⚠ ' + error.message;
    } finally {
      clearTimeout(timeout); controller = null;
      $('play-send').disabled = false; $('play-stop').disabled = true;
      const tokens = usage ? (usage.total_tokens ?? ((usage.input_tokens ?? usage.prompt_tokens ?? 0) + (usage.output_tokens ?? usage.completion_tokens ?? 0))) : null;
      const parts = [`${((performance.now() - start) / 1000).toFixed(1)} 秒`];
      if (firstByte !== undefined) parts.push(`首字 ${(firstByte / 1000).toFixed(1)} 秒`);
      parts.push(`token ${Number.isFinite(tokens) ? tokens : '未回报'}`);
      parts.push(`请求 ID ${requestId || '未建立'}`);
      $('play-meta').textContent = parts.join(' · ');
    }
  });
  loadModels();
})();
