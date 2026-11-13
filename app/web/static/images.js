(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const form = $('image-form');
  if (!form) return;
  let loading = false;
  let running = false;
  let modelVersion = 0;
  const enableSend = () => { $('image-send').disabled = running || loading || !$('image-model').value; };
  const message = (text = '') => { $('image-error').textContent = text; $('image-error').hidden = !text; };
  const errorText = (response, data) => {
    const hint = {401: '登录已失效，请刷新页面。', 402: '积分不足，请先兑换或充值。',
      403: '这把密钥没有此接口或模型的权限，或账户已停用。', 409: '账户有待处理的余额问题，请到积分页核对。',
      429: '请求太频繁或已达配额，请稍后再试。', 502: '上游回执异常，请按请求 ID 查看用量，避免立即重复生成。'};
    return `HTTP ${response.status} · ${data.error?.message || hint[response.status] || '生成失败，请保留请求 ID 查看用量。'}`;
  };
  function estimate() {
    const model = $('image-model').value;
    const promptBytes = new TextEncoder().encode($('image-prompt').value).length;
    const imageTokens = {low: 512, medium: 2048, high: 8192}[$('image-quality').value]
      * ($('image-size').value === '1024x1024' ? 1 : 1.5);
    const credits = ((promptBytes + 4096) * 5 + imageTokens * (model === 'gpt-image-1.5' ? 32 : 30)
      + (model === 'gpt-image-1.5' ? 4096 * 10 : 0)) * 720 / 1e6;
    $('image-estimate').textContent = model ? `本次冻结预算约 ${credits.toFixed(6)} 积分；最终仅按实际用量收费。` : '选择可用模型后显示冻结预算。';
  }
  async function loadModels() {
    const version = ++modelVersion;
    const previous = $('image-model').value || form.dataset.model;
    const key = $('image-key').value;
    loading = true; enableSend();
    $('image-model').replaceChildren();
    $('image-model').disabled = true;
    $('image-model-status').textContent = '正在读取这把密钥可调用的图像模型…';
    try {
      const response = await fetch(`/developer/playground/models?key_id=${encodeURIComponent(key)}&protocol=images.generations`,
        {credentials: 'same-origin', cache: 'no-store', signal: AbortSignal.timeout(15000)});
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(errorText(response, data));
      if (version !== modelVersion) return;
      const models = Array.isArray(data.data) ? data.data : [];
      $('image-model').replaceChildren(...models.map(model => {
        const option = document.createElement('option'); option.value = model.id; option.textContent = model.id; return option;
      }));
      if (models.some(model => model.id === previous)) $('image-model').value = previous;
      $('image-model-status').textContent = models.length ? `可调用 ${models.length} 个图像模型。` : '没有可用图像模型。旧密钥可重新签发，或联系管理员授权 images:generate 和图像模型。';
      $('image-model').disabled = !models.length || running;
    } catch (error) {
      if (version === modelVersion) $('image-model-status').textContent = error.name === 'TimeoutError' ? '读取模型超时，请切换密钥或刷新重试。' : error.message;
    } finally {
      if (version === modelVersion) { loading = false; enableSend(); estimate(); }
    }
  }
  $('image-download-form').addEventListener('submit', event => {
    if (!$('image-download-data').value || !$('image-download-size').value || running) event.preventDefault();
  });
  $('image-key').addEventListener('change', loadModels);
  ['image-prompt', 'image-quality', 'image-size', 'image-model'].forEach(id => $(id).addEventListener('input', estimate));
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (running || loading || !$('image-model').value || !$('image-prompt').value.trim()) return;
    const payload = {model: $('image-model').value, prompt: $('image-prompt').value,
      quality: $('image-quality').value, size: $('image-size').value, n: 1, output_format: 'png'};
    const keyId = Number($('image-key').value);
    running = true; enableSend(); message();
    ['image-key', 'image-model', 'image-quality', 'image-size', 'image-prompt'].forEach(id => { $(id).disabled = true; });
    $('generated-image').hidden = true; $('generated-image').removeAttribute('src');
    $('image-download').hidden = true; $('image-download').disabled = true;
    $('image-download-data').value = ''; $('image-download-size').value = '';
    $('image-placeholder').hidden = false;
    $('image-status').textContent = '生成中…'; $('image-usage').textContent = ''; $('image-meta').textContent = '正在等待上游回执';
    let requestId = '';
    try {
      const response = await fetch('/developer/playground/run', {method: 'POST', credentials: 'same-origin', cache: 'no-store',
        headers: {'Content-Type': 'application/json', 'Accept': 'application/json'},
        body: JSON.stringify({key_id: keyId, endpoint: 'images.generations', payload})});
      requestId = response.headers.get('x-request-id') || '';
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(errorText(response, data));
      const pending = response.headers.get('x-credit-status') === 'reconciliation_required';
      const encoded = data.data?.[0]?.b64_json;
      if (typeof encoded !== 'string' || !encoded.startsWith('iVBORw0KGgo') || !/^[A-Za-z0-9+/]*={0,2}$/.test(encoded)) throw new Error('返回的 PNG 无效，请保留请求 ID 查看用量。');
      const url = 'data:image/png;base64,' + encoded;
      $('generated-image').src = url; $('generated-image').hidden = false; $('image-placeholder').hidden = true;
      $('image-download-data').value = encoded; $('image-download-size').value = payload.size;
      $('image-download').hidden = false; $('image-download').disabled = false;
      const u = data.usage;
      $('image-usage').textContent = u ? `输入 ${u.input_tokens} token · 图片输出 ${u.output_tokens_details?.image_tokens ?? u.output_tokens} · 文本输出 ${u.output_tokens_details?.text_tokens ?? 0}` : '';
      $('image-status').textContent = pending ? '已生成 · 费用待核' : '已生成';
      if (pending) message('图片已生成；实际用量超过预留上界，已保留真实费用待核，暂未扣款。请保留请求 ID 联系管理员处理。');
    } catch (error) {
      $('image-status').textContent = '请求未完成'; message(error.message || '连接中断，请到用量页核对后再试。');
    } finally {
      $('image-meta').textContent = requestId ? `请求 ID：${requestId}` : '未获得请求 ID；可在用量分析查看最近调用';
      running = false;
      ['image-key', 'image-model', 'image-quality', 'image-size', 'image-prompt'].forEach(id => { $(id).disabled = false; });
      enableSend();
    }
  });
  loadModels();
})();
