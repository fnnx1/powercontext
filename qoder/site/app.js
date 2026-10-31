const API = 'https://yes-efforts-factors-tutorials.trycloudflare.com';
const $ = (id) => document.getElementById(id);
const session = $('session');
const action = $('action');
const story = $('story');
const memory = $('memory');
const timeline = $('timeline');
const status = $('status');
let turn = 0;

async function api(path, options) {
  const res = await fetch(API + path, options);
  if (!res.ok) throw new Error(`API ${res.status}`);
  return res.json();
}

async function refresh() {
  try {
    const health = await api('/api/health');
    status.textContent = `seekdb online · ${health.beats} beats`;
  } catch (e) {
    status.textContent = 'offline · 记忆服务不可达';
  }
}

async function tell(text) {
  const value = (text || action.value).trim();
  if (!value) return;
  $('tell').disabled = true;
  try {
    const data = await api('/api/story/continue', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ session_id: session.value.trim() || 'demo', action: value }),
    });
    turn = data.turn;
    $('turn').textContent = `第 ${data.turn} 轮`;
    story.textContent = data.story;
    memory.textContent = data.memory_used
      ? '记忆：老人翻开了账本，提起了你上一轮说过的话。'
      : '记忆：这是账本的第一页。';
    const li = document.createElement('li');
    li.innerHTML = `<strong>TURN ${String(data.turn).padStart(2, '0')} · ${value}</strong>${data.story}`;
    if (timeline.querySelector('.empty')) timeline.innerHTML = '';
    timeline.prepend(li);
    action.value = '';
    await refresh();
  } catch (e) {
    memory.textContent = `记忆服务异常：${e.message}。请稍后再试。`;
  } finally {
    $('tell').disabled = false;
  }
}

$('tell').addEventListener('click', () => tell());
action.addEventListener('keydown', (e) => { if (e.key === 'Enter') tell(); });
document.querySelectorAll('[data-action]').forEach((b) => b.addEventListener('click', () => tell(b.dataset.action)));
$('reset').addEventListener('click', () => {
  session.value = `traveler-${Math.random().toString(36).slice(2, 7)}`;
  turn = 0;
  $('turn').textContent = '第 0 轮';
  story.textContent = '一位新的旅人推门进来。酒馆的火还没灭。';
  memory.textContent = '记忆：等待第一句话…';
  timeline.innerHTML = '<li class="empty">新的账本，新的故事。</li>';
});
refresh();
