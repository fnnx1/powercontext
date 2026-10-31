import { pipeline, env } from 'https://cdn.jsdelivr.net/npm/@xenova/transformers@2.17.2';

const backendEl = document.querySelector('#backend');
const statusEl = document.querySelector('#status');
const docsEl = document.querySelector('#docs');
const queryEl = document.querySelector('#query');
const resultsEl = document.querySelector('#results');

let extractor = null;
let backend = 'wasm';
let dtype = 'q8';

function hasWebGPU() {
  return Boolean(navigator.gpu);
}

async function loadModel() {
  backend = hasWebGPU() ? 'webgpu' : 'wasm';
  backendEl.textContent = `推理后端：${backend.toUpperCase()} · dtype=${dtype}`;
  statusEl.textContent = '加载本地 embedding 模型…';
  const options = { dtype, device: backend };
  try {
    extractor = await pipeline('feature-extraction', 'Xenova/all-MiniLM-L6-v2', options);
    statusEl.textContent = '模型已加载';
  } catch (error) {
    if (backend === 'webgpu') {
      backend = 'wasm';
      backendEl.textContent = `推理后端：WASM（WebGPU 不可用，已自动降级） · dtype=${dtype}`;
      extractor = await pipeline('feature-extraction', 'Xenova/all-MiniLM-L6-v2', { dtype, device: 'wasm' });
      statusEl.textContent = '已降级到 WASM 并加载模型';
    } else {
      throw error;
    }
  }
}

async function embed(texts) {
  const output = await extractor(texts, { pooling: 'mean', normalize: true });
  return output.tolist();
}

async function api(path, body) {
  const response = await fetch(path, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || response.statusText);
  return data;
}

document.querySelector('#sample').addEventListener('click', () => {
  docsEl.value = [
    'OceanBase 是一款面向 AI 应用的分布式数据库，支持向量与全文检索。',
    'SeekDB 提供嵌入式部署方式，可以在本地保存文档和向量。',
    'PowerContext 为 AI Agent 保存可复用的记忆与任务上下文。',
    '混合检索同时使用语义向量和关键词倒排索引，能够兼顾召回与精确匹配。',
  ].join('\n');
});

document.querySelector('#import').addEventListener('click', async (event) => {
  event.target.disabled = true;
  try {
    if (!extractor) await loadModel();
    const lines = docsEl.value.split('\n').map((x) => x.trim()).filter(Boolean);
    if (!lines.length) throw new Error('请先输入至少一行文本');
    statusEl.textContent = `本地向量化 ${lines.length} 篇文档…`;
    const vectors = await embed(lines);
    const documents = lines.map((text, i) => ({ title: `文档 ${i + 1}`, text, embedding: vectors[i] }));
    const data = await api('/api/docs', { documents });
    statusEl.textContent = `已写入 seekdb：${data.inserted} 篇`;
  } catch (error) {
    statusEl.textContent = `失败：${error.message}`;
  } finally {
    event.target.disabled = false;
  }
});

document.querySelector('#search').addEventListener('click', async (event) => {
  event.target.disabled = true;
  try {
    if (!extractor) await loadModel();
    const query = queryEl.value.trim();
    if (!query) throw new Error('请输入查询');
    const [vector] = await embed([query]);
    const data = await api('/api/search', { query, embedding: vector, limit: 10 });
    resultsEl.innerHTML = '';
    if (!data.results.length) {
      resultsEl.innerHTML = '<li>没有找到结果，请先导入文档。</li>';
      return;
    }
    for (const item of data.results) {
      const li = document.createElement('li');
      const meta = document.createElement('div');
      meta.className = 'meta';
      meta.textContent = `fts=${Number(item.fts_score || 0).toFixed(3)} · distance=${item.distance == null ? 'n/a' : Number(item.distance).toFixed(3)} · hybrid=${Number(item.hybrid_score || 0).toFixed(3)}`;
      const body = document.createElement('div');
      body.textContent = item.body;
      li.append(meta, body);
      resultsEl.append(li);
    }
  } catch (error) {
    statusEl.textContent = `失败：${error.message}`;
  } finally {
    event.target.disabled = false;
  }
});

loadModel().catch((error) => {
  statusEl.textContent = `模型加载失败：${error.message}`;
});
