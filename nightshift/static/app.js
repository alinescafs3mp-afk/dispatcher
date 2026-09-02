const $ = (sel, root=document) => root.querySelector(sel);
const $$ = (sel, root=document) => [...root.querySelectorAll(sel)];
const agentIds = {grok: 'grok-architect', luna: 'codex-luna', spark: 'codex-spark'};
let state = null;
let refreshTimer = null;
const logFloors = {};

function toast(message, bad=false) {
  const el = $('#toast'); el.textContent = message; el.hidden = false;
  el.style.borderColor = bad ? 'rgba(255,107,112,.45)' : '';
  clearTimeout(el._timer); el._timer = setTimeout(() => el.hidden = true, 4200);
}
async function api(path, options={}) {
  const opts = {headers: {'Content-Type': 'application/json'}, ...options};
  const response = await fetch(path, opts);
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try { const body = await response.json(); message = body.detail || message; } catch (_) {}
    throw new Error(message);
  }
  return response.json();
}
function fmtTokens(n) {
  n = Number(n || 0); if (n < 1000) return `${n} tokens`;
  if (n < 1e6) return `${(n/1000).toFixed(n < 10000 ? 1 : 0)}k tokens`;
  return `${(n/1e6).toFixed(2)}m tokens`;
}
function latestMission() { return state?.missions?.[0] || null; }
function missionActive(status) { return ['created','recovering','running','paused','awaiting_human'].includes(status); }

function renderMission() {
  const mission = latestMission();
  const running = Boolean(state?.mission_running);
  if (!mission) {
    $('#missionTitle').textContent = 'No active takeover';
    $('#missionMeta').textContent = 'Start with forensic recovery, then let Grok route bounded work to Luna and Spark.';
    $('#missionPaths').textContent = '';
  } else {
    $('#missionTitle').textContent = `${mission.id} · ${mission.status}`;
    $('#missionMeta').textContent = mission.goal + (mission.summary ? ` · ${mission.summary}` : '');
    $('#missionPaths').textContent = [mission.integration_branch && `branch: ${mission.integration_branch}`, mission.integration_path && `worktree: ${mission.integration_path}`, mission.forensics_path && `dossier: ${mission.forensics_path}`].filter(Boolean).join('  |  ');
  }
  $('#startBtn').disabled = running;
  $('#goalInput').disabled = running;
  $('#pauseBtn').disabled = !running || mission?.status === 'paused';
  $('#resumeBtn').disabled = !running || mission?.status !== 'paused';
  $('#stopBtn').disabled = !running;
  const canResume = mission && !running && ['paused','blocked','failed'].includes(mission.status) && mission.integration_path;
  $('#resumeInterruptedBtn').hidden = !canResume;
  $('#resumeInterruptedBtn').dataset.id = canResume ? mission.id : '';
}

function quotaNode(window) {
  const row = document.createElement('div'); row.className = 'quota-row';
  const label = document.createElement('div'); label.className = 'quota-label';
  const left = window.left_percent == null ? '?' : `${Math.round(window.left_percent)}% left`;
  const reset = window.resets_at_text ? ` · ${window.resets_at_text}` : '';
  const a = document.createElement('span'); a.textContent = window.label || window.id;
  const b = document.createElement('span'); b.textContent = `${left}${reset}`;
  label.append(a,b);
  const track = document.createElement('div'); track.className = 'quota-track';
  const fill = document.createElement('div'); fill.className = 'quota-fill'; fill.style.width = `${Math.max(0, Math.min(100, window.left_percent ?? 0))}%`;
  track.append(fill); row.append(label,track); return row;
}
function renderAgents() {
  const usageMap = Object.fromEntries((state?.usage || []).map(x => [x.agent_id, x]));
  const agents = Object.fromEntries((state?.agents || []).map(x => [x.id, x]));
  for (const key of Object.keys(agentIds)) {
    const card = $(`.agent-card[data-key="${key}"]`); const agent = agents[agentIds[key]] || {};
    card.dataset.state = agent.state || 'offline';
    $('[data-role="state"]', card).textContent = agent.current_task ? `${agent.state} · ${agent.current_task}` : (agent.state || 'offline');
    const cfg = state?.config?.agents?.[key] || {};
    $('[data-role="model"]', card).textContent = agent.model || cfg.model || (key === 'luna' ? 'account-selected reserve' : 'default model');
    const effort = $('[data-role="effort"]', card);
    const effortOptions = Array.isArray(cfg.effort_options) ? cfg.effort_options : [];
    const optionSignature = effortOptions.join('|');
    if (effort.dataset.options !== optionSignature) {
      effort.replaceChildren(...effortOptions.map(value => { const option=document.createElement('option'); option.value=value; option.textContent=value; return option; }));
      effort.dataset.options = optionSignature;
    }
    effort.value = cfg.effort || effortOptions[0] || '';
    effort.disabled = !effortOptions.length || effort.dataset.saving === 'true';
    const effortBadge = $('[data-role="effort-badge"]', card); if (effortBadge) effortBadge.textContent = cfg.effort || '';
    const u = usageMap[agentIds[key]] || {}; $('[data-role="usage"]', card).textContent = fmtTokens(Number(u.input_tokens||0)+Number(u.output_tokens||0));
    const zone = $('[data-role="quotas"]', card); zone.replaceChildren();
    const quota = state?.quotas?.[key];
    if (quota?.windows?.length) quota.windows.forEach(w => zone.append(quotaNode(w)));
    else { const empty = document.createElement('div'); empty.className='quota-empty'; empty.textContent = quota?.message || 'Limit data not loaded yet.'; zone.append(empty); }
    const consoleEl = $('[data-role="console"]', card);
    const nearBottom = consoleEl.scrollHeight - consoleEl.scrollTop - consoleEl.clientHeight < 50;
    const floor = Number(logFloors[key] || 0);
    const logs = (state?.logs?.[agentIds[key]] || []).filter(x => Number(x.seq || 0) > floor);
    consoleEl.textContent = logs.map(x => `[${x.created_at.slice(11,19)}] ${x.stream.padEnd(10)} ${x.text}`).join('\n');
    if (nearBottom) consoleEl.scrollTop = consoleEl.scrollHeight;
  }
}
function renderTasks() {
  const tasks = state?.tasks || []; $('#taskCount').textContent = tasks.length;
  const body = $('#taskRows'); body.replaceChildren();
  if (!tasks.length) { const tr=document.createElement('tr'),td=document.createElement('td'); td.colSpan=5;td.className='empty';td.textContent='No task packets yet.';tr.append(td);body.append(tr); }
  else for (const task of tasks) {
    const tr=document.createElement('tr');
    const values=[task.title || task.id, task.worker, task.status, task.risk, task.attempt];
    values.forEach((v,i)=>{ const td=document.createElement('td'); td.textContent=v ?? ''; if(i===2) td.className='task-state'; if(i===3) td.className=`risk-${task.risk}`; tr.append(td); }); body.append(tr);
  }
  const pending = tasks.find(x => x.status === 'awaiting_human'); const gate = $('#humanGate'); gate.replaceChildren(); gate.hidden = !pending;
  if (pending) {
    const h=document.createElement('h4');h.textContent=`Human gate: ${pending.title}`;
    const p=document.createElement('div');p.className='muted-text';p.textContent=(pending.review?.summary || 'Grok requested operator approval') + ` · risk ${pending.risk}`;
    const actions=document.createElement('div');actions.className='gate-actions';
    const yes=document.createElement('button');yes.className='primary';yes.textContent='Approve integration';yes.onclick=()=>decide(pending.id,true);
    const no=document.createElement('button');no.className='danger';no.textContent='Reject';no.onclick=()=>decide(pending.id,false);
    actions.append(yes,no);gate.append(h,p,actions);
  }
}
function renderChat() {
  const root=$('#chatMessages'); const messages=state?.chat || []; root.replaceChildren();
  if (!messages.length) { const e=document.createElement('div');e.className='empty';e.textContent='Messages to Grok appear here. Chat does not silently dispatch work.';root.append(e);return; }
  for (const msg of messages) { const bubble=document.createElement('div');bubble.className=`chat-bubble ${msg.role==='user'?'user':''}`; const who=document.createElement('small');who.textContent=msg.role==='user'?'operator':'grok architect'; const text=document.createElement('div');text.textContent=msg.text;bubble.append(who,text);root.append(bubble); }
  root.scrollTop=root.scrollHeight;
}
function render() { if (!state) return; renderMission(); renderAgents(); renderTasks(); renderChat(); }
async function refresh() { try { state=await api('/api/state'); render(); } catch(e){ toast(e.message,true); } }
function queueRefresh() { clearTimeout(refreshTimer); refreshTimer=setTimeout(refresh,120); }
async function action(path, body=null) { try { await api(path,{method:'POST',body:body===null?'{}':JSON.stringify(body)}); await refresh(); } catch(e){ toast(e.message,true); throw e; } }
async function decide(id, approved) { await action(`/api/tasks/${encodeURIComponent(id)}/decision`,{approved,note:''}); }

$('#startBtn').onclick=async()=>{ const goal=$('#goalInput').value.trim() || 'Recover the interrupted Sol and SolGoodman work, reconcile the real backlog, and carry it to completion without architectural drift.'; await action('/api/missions/start',{goal}); };
$('#pauseBtn').onclick=()=>action('/api/mission/pause'); $('#resumeBtn').onclick=()=>action('/api/mission/resume'); $('#stopBtn').onclick=()=>action('/api/mission/stop');
$('#resumeInterruptedBtn').onclick=()=>action(`/api/missions/${encodeURIComponent($('#resumeInterruptedBtn').dataset.id)}/resume`);
$('#doctorBtn').onclick=async()=>{ toast('Running CLI doctor…'); await action('/api/doctor'); toast('Doctor finished.'); };
$('#quotaBtn').onclick=async()=>{ toast('Reading subscription limits…'); await action('/api/quotas'); toast('Limits refreshed.'); };
$('#chatForm').onsubmit=async(e)=>{ e.preventDefault(); const input=$('#chatInput'),text=input.value.trim(); if(!text)return; input.value=''; try{await action('/api/chat',{text});}catch(_){input.value=text;} };
$$('[data-role="clear-log"]').forEach(btn=>btn.onclick=()=>{
  const card=btn.closest('.agent-card'), key=card.dataset.key;
  const logs=state?.logs?.[agentIds[key]] || [];
  logFloors[key]=logs.reduce((max,row)=>Math.max(max,Number(row.seq||0)),0);
  $('[data-role="console"]',card).textContent='';
});
$$('[data-role="effort"]').forEach(select=>select.onchange=async()=>{
  const card=select.closest('.agent-card'), key=card.dataset.key, effort=select.value;
  select.dataset.saving='true'; select.disabled=true;
  try {
    await api(`/api/agents/${encodeURIComponent(key)}/reasoning`, {method:'PUT', body:JSON.stringify({effort})});
    toast(`${key}: reasoning ${effort} applies on the next model turn.`);
    await refresh();
  } catch(e) { toast(e.message,true); await refresh(); }
  finally { select.dataset.saving='false'; select.disabled=false; }
});

function connect() {
  const protocol=location.protocol==='https:'?'wss':'ws'; const ws=new WebSocket(`${protocol}://${location.host}/ws`);
  ws.onopen=()=>{ const b=$('#socketBadge');b.textContent='Sol Link live';b.className='badge good'; };
  ws.onmessage=(event)=>{ try{ const msg=JSON.parse(event.data); if(msg.type==='state.snapshot'){state=msg.payload;render();} else queueRefresh(); }catch(_){} };
  ws.onclose=()=>{ const b=$('#socketBadge');b.textContent='reconnecting';b.className='badge bad';setTimeout(connect,1800); };
  ws.onerror=()=>ws.close();
}
refresh(); connect(); setInterval(refresh,10000);
