const messages = document.querySelector('#messages');
const welcome = document.querySelector('#welcome');
const form = document.querySelector('#chat-form');
const input = document.querySelector('#question');
const send = document.querySelector('#send');
const newChat = document.querySelector('#new-chat');
const notice = document.querySelector('#notice');
const product = document.querySelector('#product');
let busy = false;

async function api(path, body) {
  const response = await fetch(path, body === undefined ? {} : {
    method: 'POST', headers: {'Content-Type': 'application/json', 'X-Wang-Client': 'web'},
    body: JSON.stringify(body)
  });
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '请求未成功，请检查输入后重试。');
  return data;
}

function bubble(role, text, sources = []) {
  const article = document.createElement('article');
  article.className = `message ${role}`;
  const label = document.createElement('div');
  label.className = 'message-label';
  label.textContent = role === 'user' ? '你' : 'wang';
  const content = document.createElement('div');
  content.className = 'message-content';
  // Keep evidence in the API/history while hiding citation markers in buyer replies.
  const visibleText = role === 'assistant'
    ? sources.reduce((answer, source) => answer.replaceAll(`[${source.id}]`, ''), text).trim()
    : text;
  content.textContent = visibleText; // Model and document text never become executable HTML.
  article.append(label, content);
  messages.append(article);
  messages.scrollTop = messages.scrollHeight;
  return article;
}

function setBusy(value) {
  busy = value;
  send.disabled = value;
  newChat.disabled = value;
  input.disabled = value;
  product.disabled = value;
  document.querySelectorAll('[data-question]').forEach(button => button.disabled = value);
}

form.addEventListener('submit', async event => {
  event.preventDefault();
  const question = input.value.trim();
  if (!question || busy) return;
  setBusy(true);
  notice.textContent = '';
  welcome.hidden = true;
  const questionBubble = bubble('user', question);
  const waiting = bubble('assistant', '正在核对商品资料与店铺政策…');
  input.value = '';
  try {
    const result = await api('/api/chat', {message: question, product_id: product.value});
    product.value = result.product_id || '';
    waiting.remove();
    bubble('assistant', result.answer, result.sources);
  } catch (error) {
    waiting.remove();
    questionBubble.remove();
    input.value = question;
    notice.textContent = error.message + ' 问题已保留，可重新发送。';
  } finally {
    setBusy(false);
    input.focus();
  }
});

input.addEventListener('keydown', event => {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    form.requestSubmit();
  }
});
document.querySelectorAll('[data-question]').forEach(button => button.addEventListener('click', () => {
  if (busy) return;
  input.value = button.dataset.question;
  input.focus();
}));
newChat.addEventListener('click', async () => {
  if (busy) return;
  setBusy(true);
  try {
    await api('/api/conversations/new', {});
    messages.querySelectorAll('.message').forEach(message => message.remove());
    welcome.hidden = false;
    input.value = '';
    product.value = '';
    notice.textContent = '';
  } catch (error) { notice.textContent = error.message; }
  finally { setBusy(false); input.focus(); }
});

async function init() {
  setBusy(true);
  try {
    const data = await api('/api/bootstrap');
    document.querySelector('#shop-name').textContent = data.shop.name;
    document.querySelector('#shop-badge').textContent = data.shop.is_demo ? '演示商品与政策' : '店铺资料问答';
    for (const item of data.products) {
      const scopeOption = document.createElement('option');
      scopeOption.value = item.id; scopeOption.textContent = item.name;
      document.querySelector('#kb-scope').append(scopeOption);
      const option = document.createElement('option');
      option.value = item.id;
      option.textContent = item.name;
      product.append(option);
    }

    if (data.history.length) product.value = data.history.at(-1).product_id || '';
    document.querySelector('#mode').textContent = data.mode === 'deepseek' ? 'DeepSeek 模型模式' : '本地资料检索模式';
    if (data.mode !== 'deepseek') notice.textContent = '未启用大模型，目前仅显示商品与店铺资料摘录。';
    if (data.history.length) welcome.hidden = true;
    for (const turn of data.history) {
      bubble('user', turn.question);
      bubble('assistant', turn.answer, turn.sources);
    }
  } catch (error) { notice.textContent = error.message + ' 请刷新页面重试。'; }
  finally { setBusy(false); }
}
init();

const kbStatus = document.querySelector('#kb-status');
const kbImport = document.querySelector('#kb-import');
function renderDocuments(documents) {
  const container = document.querySelector('#kb-documents');
  container.replaceChildren();
  for (const doc of documents) {
    const row = document.createElement('div');
    row.textContent = doc.title + (doc.imported ? ' · 已索引' : ' · 内置');
    if (doc.imported) {
      const remove = document.createElement('button');
      remove.textContent = '移除';
      remove.addEventListener('click', async () => {
        if (!window.confirm('从知识库移除这份资料？原始文件不会删除，可以重新导入。')) return;
        remove.disabled = true;
        try {
          const result = await api('/api/knowledge/' + doc.id + '/remove', {});
          renderDocuments(result.documents);
          kbStatus.textContent = '已移除，后续回答不再检索该资料；已有对话仍保留。';
        } catch (error) { kbStatus.textContent = error.message; remove.disabled = false; }
      });
      row.append(remove);
    }
    container.append(row);
  }
}
kbImport.addEventListener('click', async () => {
  const file = document.querySelector('#kb-file').files[0];
  if (!file || !/\.(md|txt)$/i.test(file.name)) { kbStatus.textContent = '请选择UTF-8编码的TXT或Markdown文件。'; return; }
  if (file.size > 400000) { kbStatus.textContent = '文件过大，最多400KB且正文不超过10万字。'; return; }
  kbImport.disabled = true;
  kbStatus.textContent = '正在解析并建立索引…';
  try {
    const bytes = await file.arrayBuffer();
    const content = new TextDecoder('utf-8', {fatal: true}).decode(bytes);
    const result = await api('/api/knowledge/import', {
      title: file.name, content, product_id: document.querySelector('#kb-scope').value
    });
    renderDocuments(result.documents);
    kbStatus.textContent = '导入成功，后续提问立即生效。相同文件和范围会自动去重。';
  } catch (error) { kbStatus.textContent = error.message; }
  finally { kbImport.disabled = false; }
});
api('/api/knowledge').then(data => renderDocuments(data.documents)).catch(error => { kbStatus.textContent = error.message; });

const memoryForm = document.querySelector('#memory-form');
const memoryStatus = document.querySelector('#memory-status');
function displayMemory(data) {
  for (const [key, value] of Object.entries(data.preferences)) {
    const field = memoryForm.elements.namedItem(key);
    if (field) field.value = value;
  }
}
document.querySelector('#memory-panel').addEventListener('toggle', async event => {
  if (!event.target.open) return;
  try { displayMemory(await api('/api/memory')); }
  catch (error) { memoryStatus.textContent = error.message; }
});
memoryForm.addEventListener('submit', async event => {
  event.preventDefault();
  try {
    displayMemory(await api('/api/memory', Object.fromEntries(new FormData(memoryForm))));
    memoryStatus.textContent = '已保存，后续回答会参考这些偏好。';
  } catch (error) { memoryStatus.textContent = error.message; }
});
document.querySelector('#memory-clear').addEventListener('click', async () => {
  try { displayMemory(await api('/api/memory/clear', {})); memoryStatus.textContent = '偏好已清除；已有对话不变，建议开启新对话。'; }
  catch (error) { memoryStatus.textContent = error.message; }
});
document.querySelector('#memory-export').addEventListener('click', async () => {
  try {
    const data = await api('/api/memory');
    const link = document.createElement('a');
    const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], {type: 'application/json'}));
    link.href = url; link.download = 'wang-memory.json'; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    memoryStatus.textContent = '已导出偏好，不包含会话令牌或聊天记录。';
  } catch (error) { memoryStatus.textContent = error.message; }
});
document.querySelector('#memory-import').addEventListener('click', async () => {
  const file = document.querySelector('#memory-file').files[0];
  if (!file || file.size > 10000) { memoryStatus.textContent = '请选择10KB以内的记忆JSON文件。'; return; }
  try {
    const data = JSON.parse(await file.text());
    displayMemory(await api('/api/memory/import', data));
    memoryStatus.textContent = '已导入并替换偏好，后续回答生效。';
  } catch (error) { memoryStatus.textContent = '导入失败：' + error.message; }
});

document.querySelector('#shop-import').addEventListener('click', async () => {
  const file = document.querySelector('#shop-file').files[0];
  if (!file || file.size > 400000) { kbStatus.textContent = '请选择400KB以内的店铺JSON。'; return; }
  try {
    const result = await api('/api/shop/import', JSON.parse(await file.text()));
    kbStatus.textContent = '已保存' + result.name + '，刷新后载入商品；建议开启新对话。';
  } catch (error) { kbStatus.textContent = error.message; }
});
