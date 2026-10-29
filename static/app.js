const state = { user: null, page: 'ask', answer: null, chatId: null, documents: [] };
const $ = id => document.getElementById(id);
const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

async function api(path, method = 'GET', body) {
  const options = { method, credentials: 'same-origin', headers: {} };
  if (body !== undefined) { options.headers['Content-Type'] = 'application/json'; options.body = JSON.stringify(body); }
  const response = await fetch(path, options);
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || `Request failed (${response.status})`);
  return result;
}

function showLogin() {
  state.user = null;
  state.answer = null;
  state.chatId = null;
  $('login-password').value = '';
  $('profile-form').reset();
  $('answer-card').classList.add('hidden');
  $('chat-list').innerHTML = '';
  $('conversation-history').innerHTML = '';
  $('conversation-history').classList.add('hidden');
  $('profile-screen').classList.add('hidden');
  $('login').classList.remove('hidden'); $('app').classList.add('hidden');
}

function showProfile() {
  $('login').classList.add('hidden');
  $('app').classList.add('hidden');
  $('profile-screen').classList.remove('hidden');
  $('profile-demo-status').classList.add('hidden');
  $('profile-form').reset();
  $('profile-name').focus();
}

function showApp(user) {
  state.user = user;
  $('profile-screen').classList.add('hidden');
  $('login').classList.add('hidden'); $('app').classList.remove('hidden');
  $('user-name').textContent = user.name;
  $('user-dept').textContent = user.role === 'admin' ? 'Admin' : 'Customer';
  $('avatar').textContent = user.name[0];
  $('top-role').textContent = user.role === 'admin' ? 'ADMIN' : 'CUSTOMER';
  document.querySelectorAll('.admin-only').forEach(el => {
    if (user.role !== 'admin') el.classList.add('hidden');
    else if (el.id !== 'upload-panel') el.classList.remove('hidden');
  });
  $('upload-panel').classList.add('hidden');
  loadSuggestions();
  loadChats();
  showPage(user.role === 'admin' ? 'analytics' : 'ask');
}

function showPage(page) {
  if (page === 'analytics' && state.user?.role !== 'admin') return;
  state.page = page;
  document.querySelectorAll('.page').forEach(el => el.classList.toggle('active', el.id === `page-${page}`));
  document.querySelectorAll('.nav-item').forEach(el => el.classList.toggle('active', el.dataset.page === page));
  if (page === 'knowledge') loadDocuments();
  if (page === 'connectors') loadConnectors();
  if (page === 'analytics') loadAnalytics();
}

function loadSuggestions() {
  const all = [
    'What warranty does the IFB CI1832D223GN1 3-star AC have?',
    'What warranty does the IFB CI1852D223GN1 5-star AC have?',
    'What conditions apply to IFB AC five-year Super Warranty?'
  ];
  const list = $('suggestion-list'); list.innerHTML = '';
  all.forEach(question => {
    const button = document.createElement('button'); button.textContent = question;
    button.addEventListener('click', () => { $('question').value = question; $('question').focus(); });
    list.appendChild(button);
  });
}

async function loadChats() {
  try {
    const { chats } = await api('/api/chats');
    $('chat-list').innerHTML = chats.length ? chats.map(chat => `<button type="button" data-chat-id="${escapeHtml(chat.id)}" class="chat-link ${chat.id === state.chatId ? 'active' : ''}">${escapeHtml(chat.title)}</button>`).join('') : '<p>No chats yet</p>';
  } catch (error) { $('chat-list').textContent = error.message; }
}

function renderHistory(messages) {
  const panel = $('conversation-history');
  panel.innerHTML = messages.map(item => `<article class="history-turn"><strong>You asked</strong><p>${escapeHtml(item.question)}</p><strong>Assistant</strong><p>${escapeHtml(item.answer)}</p><div class="history-sources">${item.citations.map(source => `<button type="button" data-doc-id="${escapeHtml(source.document_id)}">[${source.number}] ${escapeHtml(source.title)}</button>`).join('')}</div></article>`).join('');
  panel.classList.toggle('hidden', !messages.length);
}

async function loadHistory(excludeId = null) {
  if (!state.chatId) { renderHistory([]); return; }
  const data = await api(`/api/chats/${state.chatId}`);
  renderHistory(data.messages.filter(item => item.id !== excludeId));
}

async function openChat(id) {
  state.chatId = id;
  state.answer = null;
  $('answer-card').classList.add('hidden');
  $('question').value = '';
  showPage('ask');
  try { await loadHistory(); await loadChats(); }
  catch (error) { $('conversation-history').textContent = error.message; }
}

function newChat() {
  state.chatId = null;
  state.answer = null;
  $('question').value = '';
  $('answer-card').classList.add('hidden');
  renderHistory([]);
  showPage('ask');
  loadChats();
}

function renderAnswer(result) {
  state.answer = result;
  $('answer-card').classList.remove('hidden');
  $('answer-route').textContent = `${result.route} · ${result.model}`;
  $('answer-text').textContent = result.answer;
  $('answer-confidence').textContent = result.no_answer ? 'Evidence: insufficient' : `Retrieval signal: ${Math.round(result.confidence * 100)}%`;
  $('answer-latency').textContent = `${result.latency_ms} ms`;
  $('feedback-saved').classList.add('hidden');
  const list = $('source-list'); list.innerHTML = '';
  if (result.citations.length) {
    const title = document.createElement('h3'); title.textContent = `Sources used · ${result.citations.length}`; list.appendChild(title);
    result.citations.forEach(source => {
      const card = document.createElement('div'); card.className = 'source-card';
      card.innerHTML = `<button type="button">[${source.number}] ${escapeHtml(source.title)} · ${escapeHtml(source.section)} ↗</button><p>${escapeHtml(source.snippet.slice(0,260))}${source.snippet.length > 260 ? '…' : ''}</p><small>${escapeHtml(source.classification)} · Effective ${escapeHtml(source.effective_date || 'date not specified')}</small>`;
      card.querySelector('button').addEventListener('click', () => openDocument(source.document_id));
      list.appendChild(card);
    });
  }
  $('answer-card').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

async function ask(event) {
  event.preventDefault();
  const question = $('question').value.trim();
  if (!question) return;
  $('query-error').classList.add('hidden');
  const button = $('ask-button'); button.disabled = true; button.textContent = 'Finding evidence…';
  try {
    const result = await api('/api/query', 'POST', { question, department: $('department-filter').value, chat_id: state.chatId });
    state.chatId = result.chat_id;
    renderAnswer(result);
    await loadHistory(result.id);
    await loadChats();
  }
  catch (error) { $('query-error').textContent = error.message; $('query-error').classList.remove('hidden'); }
  finally { button.disabled = false; button.innerHTML = 'Ask assistant <span>→</span>'; }
}

async function openDocument(id) {
  try {
    const { document: doc } = await api(`/api/documents/${id}`);
    if (!doc) throw new Error('Source is unavailable to this account.');
    $('dialog-title').textContent = doc.title;
    $('dialog-meta').innerHTML = `<span>${escapeHtml(doc.department)}</span><span>${escapeHtml(doc.classification)}</span><span>Owner: ${escapeHtml(doc.owner)}</span><span>Effective: ${escapeHtml(doc.effective_date || 'not specified')}</span>`;
    $('dialog-content').textContent = doc.content;
    $('source-dialog').showModal();
  } catch (error) { alert(error.message); }
}

async function loadDocuments() {
  const grid = $('document-grid'); grid.innerHTML = '<p class="muted">Loading approved documents…</p>';
  try {
    state.documents = (await api('/api/documents')).documents;
    grid.innerHTML = '';
    state.documents.forEach(doc => {
      const card = document.createElement('article'); card.className = 'doc-card';
      card.innerHTML = `<div class="doc-top"><span class="doc-icon">▤</span><span class="scope">${escapeHtml(doc.classification)}</span></div><h2>${escapeHtml(doc.title)}</h2><p>${escapeHtml(doc.owner)} · ${escapeHtml(doc.document_type)}</p><div class="doc-footer"><span>${escapeHtml(doc.department)} · ${escapeHtml(doc.status)}</span><button type="button">Open source →</button></div>`;
      card.querySelector('button').addEventListener('click', () => openDocument(doc.id)); grid.appendChild(card);
    });
    if (!state.documents.length) grid.innerHTML = state.user.role === 'admin'
      ? '<div class="empty-library"><strong>Your knowledge bank is empty.</strong><p>Add a shop document to give the assistant information it can cite.</p></div>'
      : '<div class="empty-library"><strong>No shop documents yet.</strong><p>The assistant can answer from the knowledge bank after the Admin adds documents.</p></div>';
  } catch (error) { grid.textContent = error.message; }
}

async function loadConnectors() {
  const grid = $('connector-grid');
  try {
    const { connectors } = await api('/api/connectors');
    grid.innerHTML = '';
    connectors.forEach(connector => {
      const card = document.createElement('article'); card.className = 'connector-card';
      card.innerHTML = `<span class="connector-symbol">⇄</span><div><h2>${escapeHtml(connector.name)}</h2><p>${escapeHtml(connector.protocol)} · ${escapeHtml(connector.tools.join(', '))}</p><p>${escapeHtml(connector.scope)}</p></div><span class="status">${escapeHtml(connector.status)}</span>`;
      grid.appendChild(card);
    });
  } catch (error) { grid.textContent = error.message; }
}

function metric(label, value) { return `<div class="metric"><small>${escapeHtml(label)}</small><strong>${escapeHtml(value)}</strong></div>`; }
function barRows(items, nameKey) {
  const max = Math.max(1, ...items.map(item => item.count));
  return items.length ? items.map(item => `<div class="bar-row"><span>${escapeHtml(item[nameKey])}</span><div class="bar-track"><div class="bar-fill" style="width:${Math.max(5,item.count / max * 100)}%"></div></div><strong>${item.count}</strong></div>`).join('') : '<p class="muted">No activity yet.</p>';
}
async function loadAnalytics() {
  try {
    const data = await api('/api/analytics');
    $('metric-grid').innerHTML = metric('Total questions', data.queries)
      + metric('Answered questions', data.answered)
      + metric('No answer', data.queries - data.answered)
      + metric('Reviewed accuracy', data.reviewed_accuracy_pct === null ? 'Not rated' : `${data.reviewed_accuracy_pct}%`)
      + metric('Customer feedback', data.feedback_count)
      + metric('Positive feedback', data.positive_feedback_pct === null ? 'No ratings' : `${data.positive_feedback_pct}%`);
    $('department-bars').innerHTML = barRows(data.documents_by_department, 'department');
    $('route-bars').innerHTML = barRows(data.routes, 'route');
    $('recent-queries').innerHTML = data.recent_queries.length ? data.recent_queries.map(row => `<article class="review-card"><div class="review-header"><strong>${escapeHtml(row.question)}</strong><small>${escapeHtml(row.department)} · ${escapeHtml(row.route)}</small></div><p>${escapeHtml(row.answer)}</p><small>Sources: ${escapeHtml(row.sources || 'None')}</small>${row.no_answer ? '<div class="review-status">No answer to review</div>' : `<div class="review-actions"><span>Admin review: ${escapeHtml(row.verdict || 'Not reviewed')}</span><button type="button" data-query-id="${escapeHtml(row.id)}" data-verdict="correct" class="${row.verdict === 'correct' ? 'selected' : ''}">Correct</button><button type="button" data-query-id="${escapeHtml(row.id)}" data-verdict="incorrect" class="${row.verdict === 'incorrect' ? 'selected' : ''}">Incorrect</button></div>`}</article>`).join('') : '<p class="muted">No questions yet. Ask the assistant to populate this view.</p>';
    $('connector-events').innerHTML = data.connector_calls.length ? data.connector_calls.map(row => `<div class="table-row"><strong>${escapeHtml(row.connector)}</strong><span>${escapeHtml(row.status)}</span><span>${row.result_count} sources</span><span>${row.latency_ms} ms</span></div>`).join('') : '<p class="muted">No connector calls yet.</p>';
  } catch (error) { $('metric-grid').textContent = error.message; }
}

async function saveReview(event) {
  const button = event.target.closest('button[data-query-id][data-verdict]');
  if (!button) return;
  button.disabled = true;
  try {
    await api('/api/analytics/review', 'POST', { query_id: button.dataset.queryId, verdict: button.dataset.verdict });
    await loadAnalytics();
  } catch (error) {
    alert(error.message);
    button.disabled = false;
  }
}

async function upload(event) {
  event.preventDefault();
  const form = event.currentTarget; const file = form.elements.file.files[0];
  const message = $('upload-message'); message.textContent = 'Validating and indexing…'; message.className = 'muted';
  if (!file || file.size > 5 * 1024 * 1024) { message.textContent = 'Choose a file under 5 MB.'; message.className = 'error'; return; }
  try {
    const bytes = new Uint8Array(await file.arrayBuffer());
    let binary = ''; for (let i = 0; i < bytes.length; i += 8192) binary += String.fromCharCode(...bytes.subarray(i, i + 8192));
    const payload = Object.fromEntries(new FormData(form).entries()); delete payload.file;
    payload.filename = file.name; payload.file_base64 = btoa(binary);
    const { document: doc } = await api('/api/documents/upload', 'POST', payload);
    message.textContent = `Indexed ${doc.title} into ${doc.chunks} passages.`; message.className = 'success';
    form.reset(); loadDocuments();
  } catch (error) { message.textContent = error.message; message.className = 'error'; }
}

async function sendFeedback(rating) {
  if (!state.answer) return;
  try { await api('/api/feedback', 'POST', { query_id: state.answer.id, rating }); $('feedback-saved').classList.remove('hidden'); }
  catch (error) { $('feedback-saved').textContent = error.message; $('feedback-saved').classList.remove('hidden'); }
}

document.addEventListener('DOMContentLoaded', async () => {
  $('open-profile').addEventListener('click', showProfile);
  $('back-to-login').addEventListener('click', showLogin);
  $('profile-form').addEventListener('submit', event => {
    event.preventDefault();
    $('profile-form').reset();
    const status = $('profile-demo-status');
    status.textContent = 'This is a preview. No account was created, and your details were not saved.';
    status.classList.remove('hidden');
  });
  $('login-form').addEventListener('submit', async event => {
    event.preventDefault(); $('login-error').classList.add('hidden');
    try { const { user } = await api('/api/login', 'POST', { email: $('login-email').value, password: $('login-password').value }); showApp(user); }
    catch (error) { $('login-error').textContent = error.message; $('login-error').classList.remove('hidden'); }
  });
  $('user-button').addEventListener('click', async () => { await api('/api/logout', 'POST', {}); state.user = null; showLogin(); });
  document.querySelectorAll('.nav-item').forEach(button => button.addEventListener('click', () => showPage(button.dataset.page)));
  $('query-form').addEventListener('submit', ask);
  $('feedback-buttons').addEventListener('click', event => { if (event.target.dataset.rating) sendFeedback(Number(event.target.dataset.rating)); });
  $('show-upload').addEventListener('click', () => $('upload-panel').classList.remove('hidden'));
  $('close-upload').addEventListener('click', () => $('upload-panel').classList.add('hidden'));
  $('upload-form').addEventListener('submit', upload);
  $('refresh-analytics').addEventListener('click', loadAnalytics);
  $('recent-queries').addEventListener('click', saveReview);
  $('new-chat').addEventListener('click', newChat);
  $('chat-list').addEventListener('click', event => { const button = event.target.closest('button[data-chat-id]'); if (button) openChat(button.dataset.chatId); });
  $('conversation-history').addEventListener('click', event => { const button = event.target.closest('button[data-doc-id]'); if (button) openDocument(button.dataset.docId); });
  $('close-dialog').addEventListener('click', () => $('source-dialog').close());
  try { const { user } = await api('/api/me'); user ? showApp(user) : showLogin(); }
  catch { showLogin(); }
});
