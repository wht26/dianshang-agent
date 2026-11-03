const operation = document.querySelector('#operation');
const pathInput = document.querySelector('#api-path');
const bodyInput = document.querySelector('#api-body');
const schemaOutput = document.querySelector('#schema');
const statusOutput = document.querySelector('#status');
const resultOutput = document.querySelector('#result');
const run = document.querySelector('#run');
let operations = [];
let spec;
function resolve(value) {
  if (!value || typeof value !== 'object') return value;
  if (value.$ref) return spec.components.schemas[value.$ref.split('/').at(-1)];
  return value;
}
function showOperation() {
  const item = operations[Number(operation.value)];
  if (!item) return;
  pathInput.value = item.path;
  bodyInput.value = '{}';
  bodyInput.disabled = item.method === 'get';
  document.querySelector('#write-confirm').checked = false;
  const schema = resolve(item.definition.requestBody?.content?.['application/json']?.schema);
  schemaOutput.textContent = JSON.stringify({request:schema || null, parameters:item.definition.parameters || [],
    definitions:spec.components.schemas}, null, 2);
  statusOutput.textContent = '';
  resultOutput.textContent = '';
}
operation.addEventListener('change', showOperation);
run.addEventListener('click', async () => {
  const item = operations[Number(operation.value)];
  if (!item || run.disabled) return;
  statusOutput.textContent = '';
  resultOutput.textContent = '';
  try {
    const path = pathInput.value.trim();
    // Restrict this console to the selected local route; never send its token to an arbitrary URL.
    const pattern = '^' + item.path.split('/').map(part => part.startsWith('{') ? '[A-Za-z0-9_-]+' : part).join('/') + '$';
    if (!new RegExp(pattern).test(path)) throw new Error('请填写当前接口的路径参数，不能修改接口地址。');
    const headers = {'X-Wang-Client':'web'};
    if (path.startsWith('/api/admin/')) {
      const token = document.querySelector('#admin-token').value.trim();
      if (!token) throw new Error('请填写管理员令牌。');
      headers.Authorization = 'Bearer ' + token;
    }
    const options = {method:item.method.toUpperCase(),headers};
    if (item.method !== 'get') {
      if (!document.querySelector('#write-confirm').checked) throw new Error('请核对并确认这次写操作。');
      options.body = JSON.stringify(JSON.parse(bodyInput.value));
      headers['Content-Type'] = 'application/json';
    }
    run.disabled = true;
    const response = await fetch(path, options);
    resultOutput.textContent = JSON.stringify(await response.json(), null, 2);
    statusOutput.textContent = `HTTP ${response.status}`;
    document.querySelector('#write-confirm').checked = false;
  } catch (error) {statusOutput.textContent = error.message;}
  finally {run.disabled = false;}
});
fetch('/openapi.json').then(response => response.json()).then(data => {
  spec = data;
  for (const [path, methods] of Object.entries(spec.paths)) {
    if (!path.startsWith('/api/admin/commerce') && !path.startsWith('/api/commerce/')) continue;
    for (const [method, definition] of Object.entries(methods)) {
      if (!['get','post'].includes(method)) continue;
      const option = document.createElement('option');
      option.value = operations.length;
      option.textContent = `${method.toUpperCase()} ${path} · ${definition.summary || ''}`;
      operations.push({path,method,definition});
      operation.append(option);
    }
  }
  showOperation();
}).catch(() => {statusOutput.textContent = '接口结构读取失败，请刷新。';});
