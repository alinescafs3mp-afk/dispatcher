const $ = (sel, root=document) => root.querySelector(sel);
const $$ = (sel, root=document) => [...root.querySelectorAll(sel)];
const LOGICAL_KEYS = ['grok', 'luna', 'spark'];
const REFRESH_DELAY_MS = 120;
const SOCKET_STALE_MS = 40000;
const SOCKET_WATCH_INTERVAL_MS = 5000;
let state = null;
let refreshTimer = null;
let refreshPromise = null;
let refreshPending = false;
let stateClock = 0;
let appliedClock = 0;
let lastEventSeq = 0;
let reconnectDelay = 1000;
let reconnectTimer = null;
let activeSocket = null;
let lastSocketActivity = 0;
let profileRequestActive = false;
const logFloors = {};


const PROFILE_UI = {
  reserve: {
    word: 'резерв',
    label: 'Резервный контур',
    badge: 'РЕЗЕРВ',
    eyebrow: 'АВАРИЙНАЯ НЕПРЕРЫВНОСТЬ',
    description: 'Grok временно руководит операцией; Luna берёт основную реализацию, а Spark выполняет строго ограниченные микрозадачи, пока штатная пара Sol недоступна.',
    missionEyebrow: 'АВАРИЙНАЯ МИССИЯ',
    startLabel: 'Начать аварийный перехват',
    noMissionTitle: 'Нет активного аварийного перехвата',
    noMissionMeta: 'Восстановите прерванное состояние и поручите резервной команде завершить сверенный остаток.',
    goalPlaceholder: 'Цель аварийной миссии',
    defaultGoal: 'Восстановить прерванную работу Sol и SolGoodman, сверить реальный бэклог и довести его до завершения без архитектурного дрейфа.',
  },
  combat: {
    word: 'бой',
    label: 'Боевой контур',
    badge: 'БОЙ',
    eyebrow: 'ШТАТНАЯ РАЗРАБОТКА',
    description: 'Sol владеет архитектурой и развивающимся бэклогом, SolGoodman реализует и отлаживает, а Grok при необходимости подключается как быстрый помощник с независимым взглядом.',
    missionEyebrow: 'ШТАТНАЯ МИССИЯ РАЗРАБОТКИ',
    startLabel: 'Начать боевую миссию',
    noMissionTitle: 'Нет активной боевой миссии',
    noMissionMeta: 'Sol может сверить и дополнить бэклог, затем направить реализацию SolGoodman и опциональному Grok.',
    goalPlaceholder: 'Цель боевой миссии',
    defaultGoal: 'Сверить и улучшить текущий бэклог Friday, затем реализовать его по задачам с Sol в роли ведущего архитектора и SolGoodman в роли владельца реализации.',
  },
};

const AGENT_UI = {
  reserve: {
    grok: {lane: 'ВЕДУЩИЙ АРХИТЕКТОР', role: 'временный главный архитектор и ревьюер'},
    luna: {lane: 'ОСНОВНОЙ ИСПОЛНИТЕЛЬ', role: 'владелец реализации и отладчик'},
    spark: {lane: 'МИКРОЗАДАЧИ', role: 'исполнитель ограниченных микрозадач'},
  },
  combat: {
    grok: {lane: 'ВЕДУЩИЙ АРХИТЕКТОР', role: 'владелец архитектуры и бэклога, ревьюер и интеграционная инстанция'},
    luna: {lane: 'ВЛАДЕЛЕЦ РЕАЛИЗАЦИИ', role: 'ведущий инженер реализации и отладчик'},
    spark: {lane: 'ОПЦИОНАЛЬНЫЙ ПОМОЩНИК', role: 'быстрый помощник по реализации и альтернативный взгляд'},
  },
};

const STATE_LABELS = {
  offline: 'не в сети',
  idle: 'готов',
  recovering: 'восстановление',
  planning: 'планирование',
  working: 'работает',
  implementing: 'реализация',
  validating: 'проверка',
  reviewing: 'ревью',
  revision: 'доработка',
  waiting: 'ожидание',
  limited: 'лимит исчерпан',
  error: 'ошибка',
  stopped: 'остановлен',
  disabled: 'отключён',
  created: 'создана',
  ready: 'готово',
  running: 'выполняется',
  paused: 'на паузе',
  awaiting_human: 'ждёт решения',
  blocked: 'заблокирована',
  failed: 'сбой',
  completed: 'завершена',
  accepted: 'принято',
  rejected: 'отклонено',
  planned: 'запланировано',
};

const RISK_LABELS = {
  low: 'низкий',
  medium: 'средний',
  high: 'высокий',
  critical: 'критический',
};

const NUDGE_STATUS_LABELS = {
  queued: 'в очереди',
  delivered: 'доставлена',
  expired: 'истекла',
  sent: 'отправлена',
};

const STREAM_LABELS = {
  stdout: 'вывод',
  stderr: 'ошибка',
  assistant: 'модель',
  tool: 'инструмент',
  validation: 'проверка',
};

function uiProfile(current=profile()) {
  return PROFILE_UI[current?.id] || PROFILE_UI.reserve;
}

function uiAgent(key) {
  return AGENT_UI[profile()?.id]?.[key] || {};
}

function profileName(profileId) {
  return profileId === 'combat' ? 'Бой' : 'Резерв';
}

function localizeState(value) {
  return STATE_LABELS[String(value || '').toLowerCase()] || String(value || '');
}

function localizeRisk(value) {
  return RISK_LABELS[String(value || '').toLowerCase()] || String(value || '');
}

function pluralRu(value, forms) {
  const n = Math.abs(Math.trunc(Number(value) || 0));
  const mod100 = n % 100;
  const mod10 = n % 10;
  if (mod100 >= 11 && mod100 <= 14) return forms[2];
  if (mod10 === 1) return forms[0];
  if (mod10 >= 2 && mod10 <= 4) return forms[1];
  return forms[2];
}

function decimalRu(value) {
  return String(value).replace('.', ',');
}

function localizeQuotaLabel(value) {
  const label = String(value || '');
  const exact = {
    'Grok weekly credits': 'Недельные кредиты Grok',
    'Grok monthly credits': 'Месячные кредиты Grok',
    'Grok included credits': 'Включённые кредиты Grok',
    'Grok on-demand cap': 'Лимит Grok по требованию',
  };
  if (exact[label]) return exact[label];
  return label
    .replace(/Weekly limit/gi, 'недельный лимит')
    .replace(/5-hour limit/gi, 'лимит на 5 часов')
    .replace(/(\d+)-minute limit/gi, 'лимит на $1 мин.')
    .replace(/Primary limit/gi, 'основной лимит')
    .replace(/Secondary limit/gi, 'дополнительный лимит');
}

function localizeQuotaMessage(value) {
  const message = String(value || '');
  const exact = {
    'Agent disabled by the active profile': 'Участник отключён активным профилем.',
    'No quota windows returned by Codex app-server': 'App-server Codex не вернул окна квот.',
    'Grok billing responded, but no percentage limit was available': 'Grok ответил данными биллинга, но процентный лимит не был доступен.',
    'No structured subscription quota command is configured': 'Структурированная команда чтения квот подписки не настроена.',
    'Limit data not loaded yet.': 'Данные о лимитах ещё не загружены.',
    'This optional lane is disconnected in the active profile.': 'Этот опциональный участник отключён в активном профиле.',
  };
  if (exact[message]) return exact[message];
  if (message.startsWith('CodexAppServerError:')) {
    return `Ошибка app-server Codex:${message.slice('CodexAppServerError:'.length)}`;
  }
  if (message.startsWith('Grok ACP billing unavailable:')) {
    return `Биллинг Grok ACP недоступен:${message.slice('Grok ACP billing unavailable:'.length)}`;
  }
  return message;
}

function localizeApiError(value) {
  const message = String(value || '');
  const replacements = [
    ['A mission is already running', 'Миссия уже выполняется.'],
    ['Mission goal is too short', 'Цель миссии слишком короткая.'],
    ['No running mission to pause', 'Нет выполняющейся миссии, которую можно поставить на паузу.'],
    ['No running mission to resume', 'Нет выполняющейся миссии, которую можно продолжить.'],
    ['No running mission to stop', 'Нет выполняющейся миссии, которую можно остановить.'],
    ['Mission is not paused', 'Миссия не находится на паузе.'],
    ['Mission not found', 'Миссия не найдена.'],
    ['The operation cannot start while an agent turn is active', 'Операция недоступна, пока выполняется ход участника.'],
    ['The operating profile cannot change while a mission is running or paused', 'Нельзя менять профиль, пока миссия выполняется или стоит на паузе.'],
    ['Untrusted Host header', 'Недоверенный заголовок Host.'],
    ['Cross-origin control request rejected', 'Межсайтовый управляющий запрос отклонён.'],
  ];
  for (const [source, translated] of replacements) {
    if (message.includes(source)) return translated;
  }
  return message;
}


function toast(message, bad=false) {
  const el = $('#toast');
  el.textContent = message;
  el.hidden = false;
  el.style.borderColor = bad ? 'rgba(255,107,112,.45)' : '';
  clearTimeout(el._timer);
  el._timer = setTimeout(() => { el.hidden = true; }, 4200);
}

async function api(path, options={}) {
  const opts = {cache: 'no-store', ...options};
  opts.headers = {'Content-Type': 'application/json', ...(options.headers || {})};
  const response = await fetch(path, opts);
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      message = body.detail || message;
    } catch (_) {}
    throw new Error(localizeApiError(message));
  }
  return response.json();
}

function fmtTokens(n) {
  n = Number(n || 0);
  if (n < 1000) return `${n} ${pluralRu(n, ['токен', 'токена', 'токенов'])}`;
  if (n < 1e6) {
    const value = (n/1000).toFixed(n < 10000 ? 1 : 0);
    return `${decimalRu(value)} тыс. токенов`;
  }
  return `${decimalRu((n/1e6).toFixed(2))} млн токенов`;
}

function profile() {
  return state?.profile || {id: 'reserve', agents: {}, architect_key: 'grok'};
}

function profileAgent(key) {
  return profile()?.agents?.[key] || state?.config?.agents?.[key] || {};
}

function activeMission() {
  const missions = state?.missions || [];
  const activeId = state?.active_mission_id;
  if (activeId) {
    const active = missions.find(item => item.id === activeId);
    if (active) return active;
  }
  return missions.find(item => (item.profile || 'reserve') === profile().id) || null;
}

function missionTasks() {
  const mission = activeMission();
  return mission ? (state?.tasks || []).filter(task => task.mission_id === mission.id) : [];
}

function applyState(next, clock) {
  if (clock < appliedClock) return false;
  const watermark = Number(next?.event_seq ?? next?.state_revision ?? 0);
  const safeWatermark = Number.isFinite(watermark) ? Math.max(0, watermark) : 0;
  // A WebSocket event can overtake an older HTTP snapshot. Never roll the
  // dashboard backwards; immediately request a fresh authoritative snapshot.
  if (state && safeWatermark < lastEventSeq) {
    scheduleRefresh(0);
    return false;
  }
  appliedClock = clock;
  lastEventSeq = Math.max(lastEventSeq, safeWatermark);
  state = next;
  render();
  return true;
}

function renderProfile() {
  const current = profile();
  const ui = uiProfile(current);
  const locked = Boolean(state?.profile_switch_locked || profileRequestActive);
  $('#profileEyebrow').textContent = `SOL LINK / ${ui.eyebrow}`;
  $('#profileWord').textContent = ui.word;
  $('#profileDescription').textContent = ui.description;
  $('#profileBadge').textContent = ui.badge;
  $('#profileBadge').className = `badge ${current.id === 'combat' ? 'combat' : 'reserve'}`;
  $('#directiveLink').href = `/api/directive/${encodeURIComponent(current.id || 'reserve')}`;

  $$('#profileSwitch [data-profile]').forEach(button => {
    const selected = button.dataset.profile === current.id;
    button.classList.toggle('active', selected);
    button.setAttribute('aria-pressed', selected ? 'true' : 'false');
    button.disabled = locked;
  });

  const helperWrap = $('#combatGrokWrap');
  helperWrap.hidden = current.id !== 'combat';
  const helper = $('#combatGrokToggle');
  helper.checked = Boolean(current.combat_grok_enabled);
  helper.disabled = locked;
}

function renderMission() {
  const current = profile();
  const ui = uiProfile(current);
  const mission = activeMission();
  const running = Boolean(state?.mission_running);
  $('#missionEyebrow').textContent = ui.missionEyebrow;
  $('#goalInput').placeholder = ui.goalPlaceholder;
  $('#startBtn').textContent = ui.startLabel;

  if (!mission) {
    $('#missionTitle').textContent = ui.noMissionTitle;
    $('#missionMeta').textContent = ui.noMissionMeta;
    $('#missionPaths').textContent = '';
  } else {
    const missionProfile = mission.profile || 'reserve';
    $('#missionTitle').textContent = `${mission.id} · ${localizeState(mission.status)}`;
    $('#missionMeta').textContent = `[${profileName(missionProfile)}] ${mission.goal}${mission.summary ? ` · ${mission.summary}` : ''}`;
    $('#missionPaths').textContent = [
      mission.integration_branch && `ветка: ${mission.integration_branch}`,
      mission.integration_path && `рабочее дерево: ${mission.integration_path}`,
      mission.forensics_path && `досье: ${mission.forensics_path}`,
    ].filter(Boolean).join('  |  ');
  }

  $('#startBtn').disabled = running || profileRequestActive || Boolean(state?.profile_switch_locked);
  $('#goalInput').disabled = running;
  $('#pauseBtn').disabled = !running || mission?.status === 'paused';
  $('#resumeBtn').disabled = !running || mission?.status !== 'paused';
  $('#stopBtn').disabled = !running;
  const canResume = mission && !running
    && ['paused','blocked','failed'].includes(mission.status)
    && mission.integration_path;
  $('#resumeInterruptedBtn').hidden = !canResume;
  $('#resumeInterruptedBtn').dataset.id = canResume ? mission.id : '';
}

function quotaNode(window) {
  const row = document.createElement('div');
  row.className = 'quota-row';
  const label = document.createElement('div');
  label.className = 'quota-label';
  const left = window.left_percent == null ? 'неизвестно' : `${Math.round(window.left_percent)}% свободно`;
  const reset = window.resets_at_text ? ` · сброс: ${window.resets_at_text}` : '';
  const a = document.createElement('span');
  a.textContent = localizeQuotaLabel(window.label || window.id);
  const b = document.createElement('span');
  b.textContent = `${left}${reset}`;
  label.append(a, b);
  const track = document.createElement('div');
  track.className = 'quota-track';
  const fill = document.createElement('div');
  fill.className = 'quota-fill';
  fill.style.width = `${Math.max(0, Math.min(100, window.left_percent ?? 0))}%`;
  track.append(fill);
  row.append(label, track);
  return row;
}

function renderAgents() {
  const usageMap = Object.fromEntries((state?.usage || []).map(x => [x.agent_id, x]));
  const agents = Object.fromEntries((state?.agents || []).map(x => [x.id, x]));

  for (const key of LOGICAL_KEYS) {
    const card = $(`.agent-card[data-key="${key}"]`);
    const spec = profileAgent(key);
    const ui = uiAgent(key);
    const agent = agents[spec.id] || {};
    const enabled = spec.enabled !== false;
    const rawState = enabled ? (agent.state || 'offline') : 'disabled';
    card.dataset.state = rawState;
    card.classList.toggle('agent-disabled', !enabled);
    card.style.order = String((profile().slot_order || LOGICAL_KEYS).indexOf(key));

    $('[data-role="lane"]', card).textContent = ui.lane || (spec.lane || key).toUpperCase();
    $('[data-role="name"]', card).textContent = spec.display_name || key;
    $('[data-role="role"]', card).textContent = ui.role || spec.role || '';
    $('[data-role="binary"]', card).textContent = spec.binary_label || spec.physical_key || 'CLI';
    $('[data-role="model"]', card).textContent = agent.model || spec.model || 'модель выбирает wrapper';
    const stateLabel = localizeState(rawState);
    $('[data-role="state"]', card).textContent = agent.current_task
      ? `${stateLabel} · ${agent.current_task}`
      : stateLabel;

    const optional = $('[data-role="optional"]', card);
    optional.hidden = !spec.optional;
    optional.textContent = enabled ? 'опционально · включён' : 'опционально · отключён';

    const access = $('[data-role="access"]', card);
    const fullAccess = Boolean(spec.unsafe_full_access);
    access.textContent = fullAccess ? 'полный доступ' : 'песочница';
    access.className = `badge ${fullAccess ? 'full-access' : 'sandboxed'}`;
    access.title = fullAccess
      ? 'Автоматические рабочие и архитектурные ходы могут использовать полный доступ CLI к хосту. Прямой чат остаётся только для чтения.'
      : 'Этот участник остаётся внутри настроенной песочницы CLI.';

    const messageButton = $('[data-role="message-agent"]', card);
    messageButton.disabled = !enabled;
    messageButton.textContent = enabled
      ? `написать ${spec.display_name || key}`
      : 'участник отключён';

    const cfg = state?.config?.agents?.[key] || spec;
    const effort = $('[data-role="effort"]', card);
    const effortOptions = Array.isArray(cfg.effort_options) ? cfg.effort_options : [];
    const optionSignature = effortOptions.join('|');
    if (effort.dataset.options !== optionSignature) {
      effort.replaceChildren(...effortOptions.map(value => {
        const option = document.createElement('option');
        option.value = value;
        option.textContent = value;
        return option;
      }));
      effort.dataset.options = optionSignature;
    }
    effort.value = cfg.effort || effortOptions[0] || '';
    effort.disabled = !enabled || !effortOptions.length || effort.dataset.saving === 'true';
    $('[data-role="effort-badge"]', card).textContent = cfg.effort || '';

    const u = usageMap[spec.id] || {};
    $('[data-role="usage"]', card).textContent = fmtTokens(
      Number(u.input_tokens || 0) + Number(u.output_tokens || 0)
    );

    const zone = $('[data-role="quotas"]', card);
    zone.replaceChildren();
    const quota = state?.quotas?.[key];
    if (enabled && quota?.windows?.length) {
      quota.windows.forEach(window => zone.append(quotaNode(window)));
    } else {
      const empty = document.createElement('div');
      empty.className = 'quota-empty';
      empty.textContent = !enabled
        ? 'Этот опциональный участник отключён в активном профиле.'
        : localizeQuotaMessage(quota?.message || 'Limit data not loaded yet.');
      zone.append(empty);
    }

    const consoleEl = $('[data-role="console"]', card);
    const nearBottom = consoleEl.scrollHeight - consoleEl.scrollTop - consoleEl.clientHeight < 50;
    const agentId = spec.id || key;
    const floor = Number(logFloors[agentId] || 0);
    const logs = (state?.logs?.[agentId] || []).filter(row => Number(row.seq || 0) > floor);
    consoleEl.textContent = logs.map(row => {
      const time = String(row.created_at || '').slice(11, 19);
      const stream = STREAM_LABELS[String(row.stream || '').toLowerCase()] || String(row.stream || '');
      return `[${time}] ${stream.padEnd(10)} ${row.text || ''}`;
    }).join('\n');
    if (nearBottom) consoleEl.scrollTop = consoleEl.scrollHeight;
  }
}

function renderTasks() {
  const tasks = missionTasks();
  $('#taskCount').textContent = tasks.length;
  const body = $('#taskRows');
  body.replaceChildren();
  if (!tasks.length) {
    const tr = document.createElement('tr');
    const td = document.createElement('td');
    td.colSpan = 5;
    td.className = 'empty';
    td.textContent = 'Пакетов задач пока нет.';
    tr.append(td);
    body.append(tr);
  } else {
    for (const task of tasks) {
      const tr = document.createElement('tr');
      const worker = profileAgent(task.worker);
      const values = [
        task.title || task.id,
        worker.display_name || task.worker,
        localizeState(task.status),
        localizeRisk(task.risk),
        task.attempt,
      ];
      values.forEach((value, index) => {
        const td = document.createElement('td');
        td.textContent = value ?? '';
        if (index === 2) td.className = 'task-state';
        if (index === 3) td.className = `risk-${task.risk}`;
        tr.append(td);
      });
      body.append(tr);
    }
  }

  const pending = tasks.find(item => item.status === 'awaiting_human');
  const gate = $('#humanGate');
  gate.replaceChildren();
  gate.hidden = !pending;
  if (pending) {
    const h = document.createElement('h4');
    h.textContent = `Решение оператора: ${pending.title}`;
    const p = document.createElement('div');
    p.className = 'muted-text';
    p.textContent = `${pending.review?.summary || 'Архитектор запросил подтверждение оператора'} · риск: ${localizeRisk(pending.risk)}`;
    const actions = document.createElement('div');
    actions.className = 'gate-actions';
    const yes = document.createElement('button');
    yes.className = 'primary';
    yes.type = 'button';
    yes.textContent = 'Разрешить интеграцию';
    yes.onclick = () => decide(pending.id, true);
    const no = document.createElement('button');
    no.className = 'danger';
    no.type = 'button';
    no.textContent = 'Отклонить';
    no.onclick = () => decide(pending.id, false);
    actions.append(yes, no);
    gate.append(h, p, actions);
  }
}

function renderChatControls() {
  const select = $('#chatRecipient');
  const previous = select.value;
  const options = [];
  for (const key of profile().slot_order || LOGICAL_KEYS) {
    const spec = profileAgent(key);
    const ui = uiAgent(key);
    const option = document.createElement('option');
    option.value = key;
    option.textContent = `${spec.display_name || key} · ${ui.lane || spec.lane || spec.role || key}`;
    option.disabled = spec.enabled === false;
    options.push(option);
  }
  select.replaceChildren(...options);
  const previousSpec = profileAgent(previous);
  if (previous && previousSpec.id && previousSpec.enabled !== false) {
    select.value = previous;
  } else {
    select.value = profile().architect_key || 'grok';
  }
  const selected = profileAgent(select.value);
  $('#chatChannelTitle').textContent = `Связь: ${selected.display_name || select.value}`;
  $('#chatHint').textContent = selected.enabled === false
    ? 'Этот участник отключён в активном профиле.'
    : 'Авто отправляет сообщение сразу, если линия свободна, или ставит надёжную подсказку в очередь, если участник занят. Прямой разговор не редактирует код и не создаёт задачи.';
  $('#chatInput').disabled = selected.enabled === false;
  $('#chatForm button').disabled = selected.enabled === false;
}

function renderChat() {
  renderChatControls();
  const selectedKey = $('#chatRecipient').value;
  const selected = profileAgent(selectedKey);
  const root = $('#chatMessages');
  const nearBottom = root.scrollHeight - root.scrollTop - root.clientHeight < 50;
  const messages = (state?.chat || []).filter(message =>
    (message.profile || 'reserve') === profile().id
    && (message.agent_key || 'grok') === selectedKey
  );
  root.replaceChildren();
  if (!messages.length) {
    const empty = document.createElement('div');
    empty.className = 'empty';
    empty.textContent = `Прямых сообщений с ${selected.display_name || selectedKey} пока нет.`;
    root.append(empty);
    return;
  }
  for (const message of messages) {
    const bubble = document.createElement('div');
    bubble.className = `chat-bubble ${message.role === 'user' ? 'user' : ''} ${message.kind === 'nudge' ? 'nudge' : ''}`;
    const who = document.createElement('small');
    const direction = message.role === 'user'
      ? `оператор → ${selected.display_name || selectedKey}`
      : selected.display_name || selectedKey;
    const nudgeStatus = NUDGE_STATUS_LABELS[message.status] || message.status || 'в очереди';
    const status = message.kind === 'nudge' ? ` · подсказка ${nudgeStatus}` : '';
    who.textContent = `${direction}${status}`;
    const text = document.createElement('div');
    text.textContent = message.text;
    bubble.append(who, text);
    root.append(bubble);
  }
  if (nearBottom) root.scrollTop = root.scrollHeight;
}

function render() {
  if (!state) return;
  renderProfile();
  renderMission();
  renderAgents();
  renderTasks();
  renderChat();
}

async function refresh() {
  if (refreshPromise) {
    refreshPending = true;
    return refreshPromise;
  }
  refreshPending = false;
  const requestClock = ++stateClock;
  refreshPromise = (async () => {
    try {
      const next = await api('/api/state');
      applyState(next, requestClock);
    } catch (error) {
      toast(error.message, true);
    }
  })();
  try {
    await refreshPromise;
  } finally {
    refreshPromise = null;
    if (refreshPending) scheduleRefresh();
  }
}

function scheduleRefresh(delay=REFRESH_DELAY_MS) {
  refreshPending = true;
  if (refreshPromise || refreshTimer) return;
  refreshTimer = setTimeout(() => {
    refreshTimer = null;
    refresh();
  }, delay);
}

async function requestAction(path, {method='POST', body={}}={}) {
  try {
    const result = await api(path, {method, body: JSON.stringify(body)});
    refreshPending = true;
    await refresh();
    return result;
  } catch (error) {
    toast(error.message, true);
    return null;
  }
}

async function decide(id, approved) {
  return requestAction(`/api/tasks/${encodeURIComponent(id)}/decision`, {
    body: {approved, note: ''},
  });
}

async function switchProfile(profileId, combatGrokEnabled) {
  if (profileRequestActive || state?.profile_switch_locked) return;
  profileRequestActive = true;
  renderProfile();
  try {
    const result = await requestAction('/api/profile', {
      method: 'PUT',
      body: {profile: profileId, combat_grok_enabled: combatGrokEnabled},
    });
    if (result) {
      $('#goalInput').value = '';
      toast(`${PROFILE_UI[profileId]?.label || profileId}: профиль активирован.`);
    }
  } finally {
    profileRequestActive = false;
    scheduleRefresh(0);
  }
}

$('#startBtn').onclick = async () => {
  const goal = $('#goalInput').value.trim() || uiProfile().defaultGoal;
  await requestAction('/api/missions/start', {body: {goal}});
};
$('#pauseBtn').onclick = () => requestAction('/api/mission/pause');
$('#resumeBtn').onclick = () => requestAction('/api/mission/resume');
$('#stopBtn').onclick = () => requestAction('/api/mission/stop');
$('#resumeInterruptedBtn').onclick = () => requestAction(
  `/api/missions/${encodeURIComponent($('#resumeInterruptedBtn').dataset.id)}/resume`
);
$('#doctorBtn').onclick = async () => {
  toast('Проверяю CLI и авторизацию…');
  if (await requestAction('/api/doctor')) toast('Диагностика завершена.');
};
$('#quotaBtn').onclick = async () => {
  toast('Читаю лимиты подписок…');
  if (await requestAction('/api/quotas')) toast('Лимиты обновлены.');
};

$$('#profileSwitch [data-profile]').forEach(button => {
  button.onclick = () => switchProfile(
    button.dataset.profile,
    button.dataset.profile === 'combat' ? $('#combatGrokToggle').checked : null,
  );
});
$('#combatGrokToggle').onchange = event => switchProfile('combat', event.target.checked);

$('#chatRecipient').onchange = () => renderChat();
$('#chatForm').onsubmit = async event => {
  event.preventDefault();
  const input = $('#chatInput');
  const text = input.value.trim();
  if (!text) return;
  const recipient = $('#chatRecipient').value;
  const delivery = $('#chatDelivery').value;
  input.value = '';
  const result = await requestAction('/api/chat', {
    body: {text, recipient, delivery},
  });
  if (!result) {
    input.value = text;
  } else if (result.status === 'queued') {
    toast(`${result.display_name}: подсказка поставлена в очередь на следующий рабочий ход.`);
  }
};

$$('[data-role="message-agent"]').forEach(button => {
  button.onclick = () => {
    const key = button.closest('.agent-card').dataset.key;
    $('#chatRecipient').value = key;
    renderChat();
    $('#teamChat').scrollIntoView({behavior: 'smooth', block: 'center'});
    $('#chatInput').focus();
  };
});

$$('[data-role="clear-log"]').forEach(button => {
  button.onclick = () => {
    const card = button.closest('.agent-card');
    const key = card.dataset.key;
    const spec = profileAgent(key);
    const logs = state?.logs?.[spec.id] || [];
    logFloors[spec.id || key] = logs.reduce(
      (maximum, row) => Math.max(maximum, Number(row.seq || 0)),
      0,
    );
    $('[data-role="console"]', card).textContent = '';
  };
});

$$('[data-role="effort"]').forEach(select => {
  select.onchange = async () => {
    const card = select.closest('.agent-card');
    const key = card.dataset.key;
    const effort = select.value;
    select.dataset.saving = 'true';
    select.disabled = true;
    try {
      const result = await requestAction(`/api/agents/${encodeURIComponent(key)}/reasoning`, {
        method: 'PUT',
        body: {effort},
      });
      if (result) {
        toast(`${profileAgent(key).display_name || key}: уровень рассуждения ${effort} применится со следующего хода.`);
      }
    } finally {
      select.dataset.saving = 'false';
      scheduleRefresh(0);
    }
  };
});

function connect() {
  if (activeSocket && [WebSocket.CONNECTING, WebSocket.OPEN].includes(activeSocket.readyState)) {
    return;
  }
  const protocol = location.protocol === 'https:' ? 'wss' : 'ws';
  const ws = new WebSocket(`${protocol}://${location.host}/ws`);
  activeSocket = ws;
  ws.onopen = () => {
    if (activeSocket !== ws) return;
    reconnectDelay = 1000;
    lastSocketActivity = Date.now();
    const badge = $('#socketBadge');
    badge.textContent = 'Sol Link на связи';
    badge.className = 'badge good';
  };
  ws.onmessage = event => {
    if (activeSocket !== ws) return;
    lastSocketActivity = Date.now();
    try {
      const message = JSON.parse(event.data);
      if (message.type === 'state.snapshot') {
        applyState(message.payload, ++stateClock);
        return;
      }
      const sequence = Number(message.seq || 0);
      if (message.type === 'system.heartbeat') {
        if (Number.isFinite(sequence) && sequence > lastEventSeq) {
          scheduleRefresh(0);
        }
        return;
      }
      if (!Number.isFinite(sequence) || sequence <= 0) {
        scheduleRefresh();
        return;
      }
      if (sequence <= lastEventSeq) return;
      const gapDetected = lastEventSeq > 0 && sequence > lastEventSeq + 1;
      lastEventSeq = sequence;
      scheduleRefresh(gapDetected ? 0 : REFRESH_DELAY_MS);
    } catch (_) {
      scheduleRefresh(0);
    }
  };
  ws.onclose = () => {
    if (activeSocket !== ws) return;
    activeSocket = null;
    const badge = $('#socketBadge');
    badge.textContent = 'переподключение';
    badge.className = 'badge bad';
    if (reconnectTimer !== null) return;
    const jitter = 0.8 + Math.random() * 0.4;
    const delay = Math.max(250, Math.round(reconnectDelay * jitter));
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null;
      connect();
    }, delay);
    reconnectDelay = Math.min(15000, Math.round(reconnectDelay * 1.7));
  };
  ws.onerror = () => ws.close();
}

document.addEventListener('visibilitychange', () => {
  if (document.hidden) return;
  scheduleRefresh(0);
  if (!activeSocket || activeSocket.readyState >= WebSocket.CLOSING) connect();
});
window.addEventListener('online', () => {
  scheduleRefresh(0);
  connect();
});
setInterval(() => {
  const ws = activeSocket;
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  if (Date.now() - lastSocketActivity <= SOCKET_STALE_MS) return;
  const badge = $('#socketBadge');
  badge.textContent = 'связь устарела';
  badge.className = 'badge bad';
  ws.close(4000, 'heartbeat timeout');
}, SOCKET_WATCH_INTERVAL_MS);
refresh();
connect();
// Authoritative polling is only a safety net for suspended tabs, proxies, and
// operating-system sleep. Normal updates arrive through WebSocket notifications.
setInterval(() => scheduleRefresh(0), 10000);
