const loginScreen = document.getElementById('login-screen');
const dashboard = document.getElementById('dashboard');
const loginForm = document.getElementById('login-form');
const loginError = document.getElementById('login-error');
const passwordInput = document.getElementById('password-input');

const statusPill = document.getElementById('status-pill');
const statUsername = document.getElementById('stat-username');
const statUptime = document.getElementById('stat-uptime');
const statRate = document.getElementById('stat-rate');
const logConsole = document.getElementById('log-console');
const logCount = document.getElementById('log-count');

const pauseBtn = document.getElementById('pause-btn');
const tipBtn = document.getElementById('tip-btn');
const rateLimitInput = document.getElementById('rate-limit-input');
const rateWindowInput = document.getElementById('rate-window-input');
const saveRateBtn = document.getElementById('save-rate-btn');
const promptInput = document.getElementById('prompt-input');
const savePromptBtn = document.getElementById('save-prompt-btn');
const logoutBtn = document.getElementById('logout-btn');

let pollTimer = null;
let promptDirty = false;   // don't clobber what the person is typing mid-poll

function fmtUptime(seconds) {
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  return `${h}h ${m}m ${s}s`;
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (res.status === 401) {
    showLogin();
    throw new Error('unauthorized');
  }
  return res;
}

function showDashboard() {
  loginScreen.classList.add('hidden');
  dashboard.classList.remove('hidden');
  if (!pollTimer) {
    refresh();
    pollTimer = setInterval(refresh, 2500);
  }
}

function showLogin() {
  clearInterval(pollTimer);
  pollTimer = null;
  dashboard.classList.add('hidden');
  loginScreen.classList.remove('hidden');
}

async function refresh() {
  try {
    const [statusRes, logsRes] = await Promise.all([
      api('/api/status'),
      api('/api/logs'),
    ]);
    const status = await statusRes.json();
    const logs = await logsRes.json();

    statUsername.textContent = status.bot_username ? `@${status.bot_username}` : '—';
    statUptime.textContent = fmtUptime(status.uptime_seconds);
    statRate.textContent = `${status.rate_limit} msgs / ${status.rate_window}s`;

    if (status.paused) {
      statusPill.className = 'pill pill-paused';
      statusPill.innerHTML = '<span class="dot"></span> paused';
      pauseBtn.textContent = 'Resume bot';
    } else {
      statusPill.className = 'pill pill-online';
      statusPill.innerHTML = '<span class="dot"></span> online';
      pauseBtn.textContent = 'Pause bot';
    }

    if (!rateLimitInput.matches(':focus')) rateLimitInput.value = status.rate_limit;
    if (!rateWindowInput.matches(':focus')) rateWindowInput.value = status.rate_window;
    if (!promptDirty && !promptInput.matches(':focus')) promptInput.value = status.prompt_template;

    logConsole.textContent = logs.logs.join('\n');
    logCount.textContent = `${logs.logs.length} lines`;
    logConsole.scrollTop = logConsole.scrollHeight;
  } catch (err) {
    // 401 already handled inside api(); anything else, just skip this tick.
  }
}

loginForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  loginError.textContent = '';
  const res = await fetch('/api/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ password: passwordInput.value }),
  });
  if (res.ok) {
    passwordInput.value = '';
    showDashboard();
  } else {
    loginError.textContent = 'Wrong password. Try again.';
  }
});

logoutBtn.addEventListener('click', async () => {
  await fetch('/api/logout', { method: 'POST' });
  showLogin();
});

pauseBtn.addEventListener('click', async () => {
  await api('/api/pause', { method: 'POST' });
  refresh();
});

tipBtn.addEventListener('click', async () => {
  tipBtn.disabled = true;
  tipBtn.textContent = 'Sending…';
  await api('/api/trigger-tip', { method: 'POST' });
  setTimeout(() => {
    tipBtn.disabled = false;
    tipBtn.textContent = 'Send daily tip now';
  }, 2000);
});

saveRateBtn.addEventListener('click', async () => {
  await api('/api/config', {
    method: 'POST',
    body: JSON.stringify({
      rate_limit: parseInt(rateLimitInput.value, 10),
      rate_window: parseInt(rateWindowInput.value, 10),
    }),
  });
});

promptInput.addEventListener('input', () => { promptDirty = true; });

savePromptBtn.addEventListener('click', async () => {
  await api('/api/config', {
    method: 'POST',
    body: JSON.stringify({ prompt_template: promptInput.value }),
  });
  promptDirty = false;
});

// On load, find out if we're already logged in (cookie still valid).
(async () => {
  try {
    const res = await fetch('/api/status');
    if (res.ok) {
      showDashboard();
    } else {
      showLogin();
    }
  } catch {
    showLogin();
  }
})();
