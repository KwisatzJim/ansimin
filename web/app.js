let backupPreview = null, backupBusy = false;
const $ = id => document.getElementById(id);
let selectedPlaybook = '', currentJob = null, polling = null, prepared = null;
let viewedFile = null, editingFile = null, editorBusy = false;
let historyCursor = null, historyBusy = false, historyKey = null;
let inventoryFile = null, inventoryBusy = false, validatedInventory = null, inventoryBaseline = null;
let editingSchedule = null, scheduleBusy = false, scheduleRefreshing = false, scheduleRefreshed = 0;
let notificationBusy = false, notificationsRefreshed = 0, notificationSettingDirty = false;
async function api(path, body) {
  const response = await fetch(path, body === undefined ? {} : {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  const data = await response.json();
  if (!response.ok) throw Object.assign(new Error(data.error || 'Request failed'), {status: response.status, diagnostics: data.diagnostics});
  return data;
}
function element(tag, text, className) {
  const node = document.createElement(tag); node.textContent = text;
  if (className) node.className = className;
  return node;
}
const workspaceTabs = ['overview', 'playbooks', 'adhoc', 'schedules', 'history', 'backups', 'settings'];
function selectTab(name, focus = false) {
  if(!workspaceTabs.includes(name)) return;
  for(const key of workspaceTabs) {
    const selected = key === name;
    $('view-' + key).hidden = !selected;
    $('tab-' + key).setAttribute('aria-selected', String(selected));
    $('tab-' + key).tabIndex = selected ? 0 : -1;
  }
  if(focus) $('tab-' + name).focus();
}
for(const [index, name] of workspaceTabs.entries()) {
  const tab = $('tab-' + name);
  tab.onclick = () => selectTab(name);
  tab.onkeydown = event => {
    let next;
    if(event.key === 'ArrowRight') next = (index + 1) % workspaceTabs.length;
    if(event.key === 'ArrowLeft') next = (index + workspaceTabs.length - 1) % workspaceTabs.length;
    if(event.key === 'Home') next = 0;
    if(event.key === 'End') next = workspaceTabs.length - 1;
    if(next !== undefined) {event.preventDefault(); selectTab(workspaceTabs[next], true);}
  };
}
$('view-current-run').onclick = () => {selectTab('overview'); $('job-panel').scrollIntoView({behavior:'smooth', block:'start'});};
async function load() {
  try {
    const data = await api('/api/overview');
    if(!$('login').hidden) notificationSettingDirty = false;
    $('login').hidden = true; $('dashboard').hidden = false; $('logout').hidden = false; $('message').textContent = '';
    $('workspace').textContent = data.workspace;
    inventoryBaseline = data;
    const targetSelect = $('target');
    targetSelect.replaceChildren(element('option', 'Choose a target…'));
    targetSelect.firstChild.value = '';
    for (const [title, entries] of [['Groups', data.groups], ['Hosts', data.hosts]]) {
      const group = document.createElement('optgroup'); group.label = title;
      for (const entry of entries) {const option = element('option', entry.name); option.value = entry.name; group.append(option);}
      targetSelect.append(group);
    }
    const all = element('option', 'All inventory hosts'); all.value = 'all'; targetSelect.append(all);
    $('adhoc-target').replaceChildren(...Array.from(targetSelect.children, child => child.cloneNode(true)));
    const scheduleTarget = $('schedule-target').value, schedulePlaybook = $('schedule-playbook').value;
    $('schedule-target').replaceChildren(...Array.from(targetSelect.children, child => child.cloneNode(true)));
    $('schedule-target').value = scheduleTarget;
    $('schedule-playbook').replaceChildren(element('option', 'Choose a playbook…'));
    $('schedule-playbook').firstChild.value = '';
    for(const name of data.playbooks) {const option = element('option', name); option.value = name; $('schedule-playbook').append(option);}
    $('schedule-playbook').value = schedulePlaybook;
    $('stats').replaceChildren(...[[data.hosts.length, 'Hosts'], [data.groups.length, 'Groups'], [data.playbooks.length, 'Playbooks']].map(([count, title]) => {const box = element('div', '', 'stat'); box.append(element('strong', count), element('span', title)); return box;}));
    $('groups').replaceChildren(...data.groups.map(group => {const row = element('div', '', 'row'); row.append(element('strong', group.name), element('span', group.hosts.join(', ') || 'No direct hosts', 'muted')); if(group.children.length) row.append(element('span', 'Child groups: ' + group.children.join(', '), 'muted')); return row;}));
    $('hosts').replaceChildren(...data.hosts.map(host => {const row = element('div', '', 'row'); row.append(element('strong', host.name), element('span', `${host.user || 'Default user'} · ${host.connection}`, 'muted')); return row;}));
    $('playbooks').replaceChildren(...data.playbooks.map(name => {const button = element('button', name, 'playbook'); button.onclick = () => openPlaybook(name); return button;}));
    await refreshJob();
    await refreshCredential();
    if (!polling) polling = setInterval(refreshJob, 1500);
  } catch(error) {if(error.status !== 401) $('message').textContent = error.message;}
}
function signedOut() {selectTab('overview'); $('run-indicator').hidden = true; backupPreview = null; $('backup-list').replaceChildren(); $('backup-content').textContent = ''; $('backup-diff').textContent = ''; $('backup-preview').hidden = true; $('login').hidden = false; $('dashboard').hidden = true; $('logout').hidden = true; $('source').textContent = ''; $('password').value = ''; $('sudo-password').value = ''; $('stored-sudo-password').value = ''; $('stored-sudo-confirm').value = ''; $('run-dialog').close(); $('run-form').hidden = true; selectedPlaybook = ''; prepared = null; currentJob = null; historyKey = null; $('history-output').textContent = ''; $('history-detail').hidden = true; clearInterval(polling); polling = null;}
$('login-form').onsubmit = async event => {event.preventDefault(); const password = $('password').value; $('password').value = ''; try {await api('/api/login', {password}); await load();} catch(error) {$('message').textContent = error.message;}};
$('logout').onclick = async () => {try {await api('/api/logout', {}); $('stored-sudo-password').value = ''; $('stored-sudo-confirm').value = ''; signedOut();} catch(error) {$('message').textContent = error.message;}};
const modeNames = {syntax: 'Syntax check', check: 'Check mode', run: 'Run playbook'};
async function refreshJob() {
  if ($('dashboard').hidden) return;
  try {
    const {job} = await api('/api/run'); currentJob = job;
    $('job-panel').hidden = !job;
    const active = job && ['running', 'cancelling'].includes(job.status);
    $('run-indicator').hidden = !job;
    $('run-indicator').classList.toggle('is-active', Boolean(active));
    $('run-indicator-text').textContent = job ? `${active ? 'Active run' : 'Latest run'} · ${job.kind === 'adhoc' ? job.module : job.playbook} · ${job.target} · ${job.status}` : '';
    $('restore-backup').disabled = Boolean(active) || backupBusy || !backupPreview;
    $('delete-backup').disabled = backupBusy || !backupPreview;
    $('download-backup').disabled = backupBusy || !backupPreview;
    $('prepare').disabled = Boolean(active);
    $('prepare-adhoc').disabled = Boolean(active);
    $('validate-playbook').disabled = Boolean(active) || editorBusy;
    $('save-playbook').disabled = Boolean(active) || editorBusy;
    $('validate-inventory').disabled = Boolean(active) || inventoryBusy;
    $('save-inventory').disabled = Boolean(active) || inventoryBusy || !validatedInventory;
    const key = job ? `${job.id}:${job.status}:${job.finished}` : 'empty';
    if (historyKey !== key) {await refreshHistory(); historyKey = key;}
    if (Date.now() - scheduleRefreshed > 15000) await refreshSchedules();
    if (Date.now() - notificationsRefreshed > 10000) await refreshNotifications();
    if (!job) return;
    $('history-warning').hidden = !job.history_error;
    $('job-title').textContent = `${job.kind === 'adhoc' ? job.module : job.playbook} · ${job.target}`;
    const modeLabel = job.kind === 'adhoc' && job.mode === 'run' ? 'Run module' : modeNames[job.mode];
    $('job-status').textContent = `${modeLabel} · ${job.status}${job.exit_code === null ? '' : ' · exit code ' + job.exit_code}${job.schedule_name ? ' · Schedule: ' + job.schedule_name : ''} · Reference: ${job.id.slice(0,12)}`;
    const output = $('job-output'); const atBottom = output.scrollHeight - output.scrollTop - output.clientHeight < 50;
    output.textContent = job.output || 'Waiting for Ansible output…';
    if (atBottom) output.scrollTop = output.scrollHeight;
    $('truncated').hidden = !job.truncated;
    $('cancel-job').hidden = !active; $('cancel-job').disabled = job.status === 'cancelling';
  } catch(error) {if(error.status === 401) {signedOut(); $('message').textContent = 'Your session expired. Sign in to reconnect to the run.';} else $('message').textContent = error.message;}
}
$('mode').onchange = () => {$('mode-help').textContent = {syntax: 'Checks playbook syntax without executing tasks.', check: 'Predicts supported changes. Tasks that override check mode can still make changes.', run: 'Executes the playbook and can change the selected machines.'}[$('mode').value];};
$('run-form').onsubmit = event => {
  event.preventDefault();
  prepared = {playbook: selectedPlaybook, target: $('target').value, mode: $('mode').value};
  const needsPassword = $('use-sudo').checked && prepared.mode !== 'syntax';
  $('run-summary').textContent = `${modeNames[prepared.mode]}: ${prepared.playbook} on ${prepared.target === 'all' ? 'ALL INVENTORY HOSTS' : prepared.target}.`;
  $('argument-summary').hidden = true; $('argument-summary').textContent = '';
  openRunDialog(needsPassword);
};
function openRunDialog(needsPassword) {
  $('sudo-field').hidden = !needsPassword; $('sudo-password').required = needsPassword;
  $('sudo-password').value = ''; $('dialog-error').textContent = ''; $('start-run').disabled = false;
  $('run-dialog').showModal();
  if (needsPassword) $('sudo-password').focus();
}
$('adhoc-module').oninput = () => {
  $('adhoc-help').textContent = {
    'ansible.builtin.ping': 'Ping needs no arguments or sudo.',
    'ansible.builtin.command': 'Example: uptime. Shell operators such as | and > are not interpreted.',
    'ansible.builtin.shell': 'Example: uptime | cat. Shell operators are interpreted on the target.',
    'ansible.builtin.setup': 'Example: filter=ansible_distribution. Gathers system facts.',
    'ansible.builtin.apt': 'Example: update_cache=true. Usually requires sudo.',
    'ansible.builtin.service': 'Example: name=nginx state=started. Usually requires sudo.'
  }[$('adhoc-module').value] || 'Enter the module’s arguments as key=value pairs or JSON.';
};
$('adhoc-form').onsubmit = event => {
  event.preventDefault();
  prepared = {kind: 'adhoc', module: $('adhoc-module').value.trim(), arguments: $('adhoc-arguments').value,
              target: $('adhoc-target').value, mode: $('adhoc-mode').value, become: $('adhoc-sudo').checked};
  const label = prepared.mode === 'check' ? 'Check module' : 'Run module';
  $('run-summary').textContent = `${label}: ${prepared.module} on ${prepared.target === 'all' ? 'ALL INVENTORY HOSTS' : prepared.target}${prepared.become ? ' using sudo' : ''}.`;
  $('argument-summary').hidden = !prepared.arguments; $('argument-summary').textContent = prepared.arguments;
  openRunDialog(prepared.become);
};
$('dismiss-run').onclick = () => $('run-dialog').close();
$('run-dialog').addEventListener('close', () => {$('sudo-password').value = ''; prepared = null;});
$('confirm-run').onsubmit = async event => {
  event.preventDefault(); if (!prepared) return;
  const payload = {...prepared, sudo_password: $('sudo-field').hidden ? '' : $('sudo-password').value};
  $('sudo-password').value = ''; $('start-run').disabled = true;
  try {await api('/api/run', payload); $('run-dialog').close(); selectTab('overview'); $('message').textContent = ''; await refreshJob();}
  catch(error) {$('dialog-error').textContent = error.message; $('start-run').disabled = false; if(error.status === 401) signedOut();}
  finally {payload.sudo_password = '';}
};
$('cancel-job').onclick = async () => {if (!currentJob) return; try {await api('/api/run/cancel', {id: currentJob.id}); await refreshJob();} catch(error) {$('message').textContent = error.message;}};
load();

function dirtyEditor() {
  return editingFile && ($('editor-content').value !== editingFile.content || $('editor-name').value !== editingFile.name);
}
function mayLeaveEditor() {return !editorBusy && (!dirtyEditor() || window.confirm('Discard your unsaved playbook changes?'));}
function showPlaybook(file) {
  viewedFile = file; selectedPlaybook = file.name;
  $('filename').textContent = file.name; $('source').textContent = file.content;
  $('source').hidden = false; $('run-form').hidden = false; $('edit-playbook').hidden = false;
  $('editor-form').hidden = true; editingFile = null;
}
async function openPlaybook(name) {
  if (!mayLeaveEditor()) return;
  try {showPlaybook(await api('/api/playbook/' + encodeURIComponent(name)));}
  catch(error) {$('message').textContent = error.message; if(error.status === 401) signedOut();}
}
function startEditor(file) {
  if (!mayLeaveEditor()) return;
  editingFile = {...file};
  $('editor-name').value = file.name; $('editor-name').readOnly = file.revision !== null;
  $('editor-content').value = file.content;
  $('editor-form').hidden = false; $('source').hidden = true; $('run-form').hidden = true;
  $('edit-playbook').hidden = true; $('filename').textContent = file.revision === null ? 'New playbook' : file.name;
  $('editor-status').textContent = ''; $('editor-diagnostics').hidden = true;
  (file.revision === null ? $('editor-name') : $('editor-content')).focus();
}
$('edit-playbook').onclick = () => {if(viewedFile) startEditor(viewedFile);};
$('new-playbook').onclick = () => startEditor({name: '', revision: null, content: '---\n- name: My new playbook\n  hosts: all\n  gather_facts: false\n  tasks:\n    - name: Show a message\n      ansible.builtin.debug:\n        msg: "Hello from ansimin"\n'});
$('cancel-edit').onclick = () => {
  if (!mayLeaveEditor()) return;
  editingFile = null; $('editor-form').hidden = true;
  if (viewedFile) showPlaybook(viewedFile); else $('filename').textContent = '';
};
$('editor-content').addEventListener('keydown', event => {
  if (event.key === 'Tab' && !event.shiftKey) {event.preventDefault(); const field = event.target; field.setRangeText('  ', field.selectionStart, field.selectionEnd, 'end');}
});
async function checkOrSave(save) {
  if (!editingFile || editorBusy || !$('editor-form').reportValidity()) return;
  editorBusy = true; $('validate-playbook').disabled = true; $('save-playbook').disabled = true;
  $('editor-content').readOnly = true; $('editor-name').readOnly = true;
  $('editor-status').textContent = save ? 'Checking syntax before saving…' : 'Checking syntax…';
  $('editor-diagnostics').hidden = true;
  try {
    const payload = {name: $('editor-name').value, content: $('editor-content').value, revision: editingFile.revision};
    const result = await api(save ? '/api/playbook/save' : '/api/playbook/validate', payload);
    if (save) {
      showPlaybook(result);
      await load();
      $('message').textContent = result.backup ? `Saved ${result.name}. Previous version backed up as ${result.backup}.` : `Created ${result.name}. Syntax check passed.`;
    } else {
      $('editor-status').textContent = 'Syntax check passed. No file was saved.';
      $('editor-diagnostics').textContent = result.diagnostics; $('editor-diagnostics').hidden = !result.diagnostics;
    }
  } catch(error) {
    $('editor-status').textContent = error.message;
    $('editor-diagnostics').textContent = error.diagnostics || ''; $('editor-diagnostics').hidden = !error.diagnostics;
    if(error.status === 401) {signedOut(); $('message').textContent = 'Session expired. Sign in again; your editor contents are still here.';}
  } finally {
    editorBusy = false; $('editor-content').readOnly = false;
    $('editor-name').readOnly = Boolean(editingFile && editingFile.revision !== null);
    await refreshJob();
  }
}
$('editor-form').onsubmit = event => {event.preventDefault(); checkOrSave(true);};
$('validate-playbook').onclick = () => checkOrSave(false);
window.addEventListener('beforeunload', event => {if(dirtyEditor() || editorBusy || dirtyInventory() || inventoryBusy) {event.preventDefault(); event.returnValue = '';}});

function runLabel(job) {return job.kind === 'adhoc' ? job.module : job.playbook;}
function runMode(job) {return job.kind === 'adhoc' && job.mode === 'run' ? 'Run module' : modeNames[job.mode];}
function runTime(timestamp) {return new Date(timestamp * 1000).toLocaleString();}
async function refreshHistory(older = false) {
  if (historyBusy || $('dashboard').hidden) return;
  historyBusy = true; $('refresh-history').disabled = true; $('more-history').disabled = true;
  try {
    const page = await api('/api/history' + (older && historyCursor ? '?before=' + encodeURIComponent(historyCursor) : ''));
    if ($('dashboard').hidden) return;
    if (!older) $('history-list').replaceChildren();
    for (const job of page.runs) {
      const button = element('button', '', 'history-row');
      button.append(element('strong', `${runLabel(job)} · ${job.target}`), element('span', `${runTime(job.started)} · ${runMode(job)} · ${job.status}${job.schedule_name ? ' · Schedule: ' + job.schedule_name : ''} · Reference: ${job.id.slice(0,12)}`, 'muted'));
      button.onclick = () => openHistory(job.id);
      $('history-list').append(button);
    }
    historyCursor = page.cursor; $('more-history').hidden = !page.cursor;
    $('history-message').textContent = $('history-list').children.length ? '' : 'No saved runs yet. Run a playbook or an ad-hoc command to begin.';
  } catch(error) {$('history-message').textContent = error.message; if(error.status === 401) signedOut();}
  finally {historyBusy = false; $('refresh-history').disabled = false; $('more-history').disabled = false;}
}
async function openHistory(id) {
  try {
    const {job} = await api('/api/history/' + encodeURIComponent(id));
    if ($('dashboard').hidden) return;
    $('history-title').textContent = `${runLabel(job)} · ${job.target}`;
    const duration = job.finished === null ? 'In progress' : `${Math.max(0, Math.round(job.finished - job.started))} seconds`;
    $('history-status').textContent = `${runMode(job)} · ${job.status} · ${runTime(job.started)} · ${duration}${job.exit_code === null ? '' : ' · exit code ' + job.exit_code}${job.schedule_name ? ' · Schedule: ' + job.schedule_name : ''} · Reference: ${job.id.slice(0,12)}`;
    $('history-output').textContent = job.output || 'No output was recorded.';
    $('history-truncated').hidden = !job.truncated; $('history-detail').hidden = false;
    selectTab('history');
    $('history-detail').scrollIntoView({behavior:'smooth', block:'start'});
  } catch(error) {$('history-message').textContent = error.message; if(error.status === 401) signedOut();}
}
$('refresh-history').onclick = () => refreshHistory();
$('more-history').onclick = () => refreshHistory(true);
$('close-history').onclick = () => {$('history-detail').hidden = true; $('history-output').textContent = '';};

function dirtyInventory() {return inventoryFile && $('inventory-content').value !== inventoryFile.content;}
$('edit-inventory').onclick = async () => {
  if (inventoryBusy || (dirtyInventory() && !window.confirm('Discard your unsaved inventory changes?'))) return;
  try {
    const file = await api('/api/inventory');
    inventoryFile = file; validatedInventory = null;
    $('inventory-content').value = file.content; $('inventory-panel').hidden = false;
    $('inventory-status').textContent = ''; $('inventory-preview').hidden = true; $('inventory-diagnostics').hidden = true;
    $('save-inventory').disabled = true; $('inventory-content').focus();
  } catch(error) {$('message').textContent = error.message; if(error.status === 401) signedOut();}
};
$('inventory-content').oninput = () => {
  validatedInventory = null; $('save-inventory').disabled = true;
  $('inventory-preview').hidden = true; $('inventory-diagnostics').hidden = true;
  $('inventory-status').textContent = 'Draft changed. Validate inventory before saving.';
};
$('cancel-inventory').onclick = () => {
  if (inventoryBusy || (dirtyInventory() && !window.confirm('Discard your unsaved inventory changes?'))) return;
  $('inventory-panel').hidden = true; $('inventory-content').value = ''; inventoryFile = null; validatedInventory = null;
};
function inventoryChanges(result) {
  const oldHosts = new Set((inventoryBaseline?.hosts || []).map(host => host.name));
  const newHosts = new Set(result.hosts.map(host => host.name));
  const removed = [...oldHosts].filter(name => !newHosts.has(name));
  const added = [...newHosts].filter(name => !oldHosts.has(name));
  let text = `${result.hosts.length} hosts · ${result.groups.length} groups\nHosts: ${[...newHosts].join(', ')}`;
  for (const group of result.groups) text += `\n[${group.name}] ${group.hosts.join(', ')}${group.children.length ? ' · child groups: ' + group.children.join(', ') : ''}`;
  if(added.length) text += '\nAdded hosts: ' + added.join(', ');
  if(removed.length) text += '\nRemoved hosts: ' + removed.join(', ');
  return text;
}
async function updateInventory(save) {
  if (!inventoryFile || inventoryBusy || !$('inventory-form').reportValidity()) return;
  if (save && (!validatedInventory || !window.confirm('Save inventory.ini with this validated inventory?\n\n' + inventoryChanges(validatedInventory) + '\n\nThe previous file will be backed up.'))) return;
  inventoryBusy = true; $('inventory-content').readOnly = true;
  $('validate-inventory').disabled = true; $('save-inventory').disabled = true;
  $('inventory-status').textContent = save ? 'Validating and saving inventory…' : 'Validating inventory…';
  $('inventory-diagnostics').hidden = true;
  try {
    const result = await api(save ? '/api/inventory/save' : '/api/inventory/validate',
      {content: $('inventory-content').value, revision: inventoryFile.revision});
    validatedInventory = result;
    $('inventory-preview').textContent = inventoryChanges(result); $('inventory-preview').hidden = false;
    $('inventory-diagnostics').textContent = result.diagnostics; $('inventory-diagnostics').hidden = !result.diagnostics;
    if (save) {
      inventoryFile = result; validatedInventory = null;
      await load();
      $('inventory-status').textContent = `Saved inventory.ini. Previous version backed up as ${result.backup}. Host lists and target selectors have been refreshed.`;
    } else $('inventory-status').textContent = 'Validation passed. Review the preview, then save when ready.';
  } catch(error) {
    validatedInventory = null; $('inventory-preview').hidden = true;
    $('inventory-status').textContent = error.message;
    $('inventory-diagnostics').textContent = error.diagnostics || ''; $('inventory-diagnostics').hidden = !error.diagnostics;
    if(error.status === 401) {signedOut(); $('message').textContent = 'Session expired. Sign in again; your inventory draft is still here.';}
  } finally {
    inventoryBusy = false; $('inventory-content').readOnly = false;
    await refreshJob();
  }
}
$('validate-inventory').onclick = () => updateInventory(false);
$('inventory-form').onsubmit = event => {event.preventDefault(); updateInventory(true);};

let credentialBusy = false;
function showCredential(status) {
  $('credential-status').textContent = status.configured ? `Stored credential configured · updated ${runTime(status.updated)}` : 'No stored credential configured.';
  $('remove-credential').hidden = !status.configured;
  $('save-credential').textContent = status.configured ? 'Replace encrypted credential' : 'Save encrypted credential';
}
async function refreshCredential() {
  try {const status = await api('/api/scheduler-credential'); if(!$('dashboard').hidden) showCredential(status);}
  catch(error) {$('credential-status').textContent = error.message; if(error.status === 401) signedOut();}
}
$('credential-form').onsubmit = async event => {
  event.preventDefault(); if(credentialBusy) return;
  if($('stored-sudo-password').value !== $('stored-sudo-confirm').value) {$('credential-message').textContent = 'The sudo passwords do not match.'; return;}
  const payload = {password: $('stored-sudo-password').value, confirm_password: $('stored-sudo-confirm').value};
  $('stored-sudo-password').value = ''; $('stored-sudo-confirm').value = '';
  credentialBusy = true; $('save-credential').disabled = true; $('remove-credential').disabled = true;
  try {
    showCredential(await api('/api/scheduler-credential/save', payload));
    $('credential-message').textContent = 'Encrypted credential saved. No jobs were started.';
  } catch(error) {$('credential-message').textContent = error.message; if(error.status === 401) signedOut();}
  finally {payload.password = ''; payload.confirm_password = ''; credentialBusy = false; $('save-credential').disabled = false; $('remove-credential').disabled = false;}
};
$('remove-credential').onclick = async () => {
  if(credentialBusy || !window.confirm('Remove the stored sudo credential? Unattended runs that require it will be unavailable.')) return;
  credentialBusy = true; $('save-credential').disabled = true; $('remove-credential').disabled = true;
  try {
    showCredential(await api('/api/scheduler-credential/remove', {}));
    $('credential-message').textContent = 'Stored credential removed.';
  } catch(error) {$('credential-message').textContent = error.message; if(error.status === 401) signedOut();}
  finally {credentialBusy = false; $('save-credential').disabled = false; $('remove-credential').disabled = false;}
};

function scheduleDate(timestamp, zone) {
  return new Intl.DateTimeFormat(undefined, {dateStyle: 'medium', timeStyle: 'short', timeZone: zone}).format(new Date(timestamp * 1000));
}
const weekdayNames = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'];
function scheduleSummary(item) {
  return `${item.name}: ${item.playbook} on ${item.target === 'all' ? 'ALL INVENTORY HOSTS' : item.target}, ${item.cadence === 'weekly' ? 'every ' + weekdayNames[item.weekday] : 'daily'} at ${item.time} (${item.timezone}), ${item.mode === 'check' ? 'check mode' : 'normal execution'}, ${item.use_credential ? 'using the stored sudo credential' : 'without a sudo credential'}.`;
}
async function refreshSchedules() {
  if(scheduleRefreshing || $('dashboard').hidden) return;
  scheduleRefreshing = true; $('refresh-schedules').disabled = true;
  try {
    const data = await api('/api/schedules');
    if($('dashboard').hidden) return;
    $('schedule-list').replaceChildren();
    if(!data.schedules.length) $('schedule-list').append(element('p', 'No schedules yet.', 'muted'));
    for(const item of data.schedules) {
      const row = element('div', '', 'schedule-row');
      row.append(element('strong', item.name), element('p', scheduleSummary(item), 'muted'));
      row.append(element('p', 'Reference: ' + item.id.slice(0,12), 'muted'));
      row.append(element('p', item.enabled ? `Enabled · next run: ${scheduleDate(item.next_run, item.timezone)} (${item.timezone})` : 'Paused · no recurring runs', 'muted'));
      if(item.last_status) row.append(element('p', `Last attempt: ${item.last_status.replaceAll('_', ' ')}${item.last_attempt ? ' · ' + runTime(item.last_attempt) : ''}`, 'muted'));
      const controls = element('div', '', 'editor-buttons');
      for(const [label, action] of [['Edit', 'edit'], [item.enabled ? 'Pause' : 'Enable', 'toggle'], ['Run once now', 'run'], ['Delete', 'remove']]) {
        const button = element('button', label, 'secondary'); button.type = 'button'; button.disabled = scheduleBusy;
        button.onclick = () => scheduleAction(item, action); controls.append(button);
      }
      if(item.last_run_id) {const button = element('button', 'View last output', 'secondary'); button.type = 'button'; button.onclick = () => openHistory(item.last_run_id); controls.append(button);}
      row.append(controls); $('schedule-list').append(row);
    }
    if(data.scheduler_error) $('schedule-message').textContent = 'The scheduler encountered an error. Check storage and the service on the controller.';
    scheduleRefreshed = Date.now();
  } catch(error) {$('schedule-message').textContent = error.message; if(error.status === 401) signedOut();}
  finally {scheduleRefreshing = false; $('refresh-schedules').disabled = false;}
}
function resetScheduleForm() {
  editingSchedule = null; $('schedule-form').reset(); $('schedule-weekday-label').hidden = true;
  $('save-schedule').textContent = 'Save paused schedule'; $('cancel-schedule-edit').hidden = true;
}
async function scheduleAction(item, action) {
  if(scheduleBusy) return;
  if(action === 'edit') {
    editingSchedule = {id: item.id, version: item.version};
    for(const [field, value] of Object.entries({name:item.name, playbook:item.playbook, target:item.target, cadence:item.cadence, weekday:String(item.weekday), time:item.time, timezone:item.timezone, mode:item.mode})) $('schedule-' + field).value = value;
    $('schedule-credential').checked = item.use_credential; $('schedule-enabled').checked = item.enabled;
    $('schedule-weekday-label').hidden = item.cadence !== 'weekly';
    $('save-schedule').textContent = item.enabled ? 'Save enabled schedule' : 'Save paused schedule';
    $('cancel-schedule-edit').hidden = false; $('schedule-name').focus(); return;
  }
  if(action === 'toggle' && !window.confirm(`${item.enabled ? 'Pause' : 'Enable'} recurring runs?\n\n${scheduleSummary(item)}`)) return;
  if(action === 'run' && !window.confirm(`Run this schedule ONCE NOW?\n\n${scheduleSummary(item)}\n\nThis does not enable or change its recurring time.`)) return;
  if(action === 'remove' && !window.confirm(`Delete schedule "${item.name}"? Its saved run history will remain.`)) return;
  scheduleBusy = true;
  try {
    await api('/api/schedules/' + action, {id:item.id, version:item.version, ...(action === 'toggle' ? {enabled:!item.enabled} : {})});
    if(editingSchedule?.id === item.id) resetScheduleForm();
    $('schedule-message').textContent = {toggle:item.enabled ? 'Schedule paused.' : 'Schedule enabled.', run:'Scheduled playbook started. See the live output or run history.', remove:'Schedule deleted. Run history retained.'}[action];
    await refreshSchedules(); if(action === 'run') await refreshJob();
  } catch(error) {$('schedule-message').textContent = error.message; if(error.status === 401) signedOut();}
  finally {scheduleBusy = false; await refreshSchedules();}
}
$('schedule-cadence').onchange = () => {$('schedule-weekday-label').hidden = $('schedule-cadence').value !== 'weekly';};
$('schedule-enabled').onchange = () => {$('save-schedule').textContent = $('schedule-enabled').checked ? 'Save enabled schedule' : 'Save paused schedule';};
$('cancel-schedule-edit').onclick = resetScheduleForm;
$('refresh-schedules').onclick = refreshSchedules;
$('schedule-form').onsubmit = async event => {
  event.preventDefault(); if(scheduleBusy) return;
  const spec = {...(editingSchedule || {}), name:$('schedule-name').value, playbook:$('schedule-playbook').value,
    target:$('schedule-target').value, cadence:$('schedule-cadence').value, weekday:Number($('schedule-weekday').value),
    time:$('schedule-time').value, timezone:$('schedule-timezone').value.trim(), mode:$('schedule-mode').value,
    use_credential:$('schedule-credential').checked, enabled:$('schedule-enabled').checked};
  if(spec.enabled && !window.confirm('Save and ENABLE recurring runs?\n\n' + scheduleSummary(spec))) return;
  scheduleBusy = true; $('save-schedule').disabled = true;
  try {
    const saved = await api('/api/schedules/save', spec);
    resetScheduleForm();
    $('schedule-message').textContent = saved.enabled ? `Schedule enabled. Next run: ${scheduleDate(saved.next_run, saved.timezone)} (${saved.timezone}).` : 'Schedule saved paused. You can run it once for testing before enabling recurring runs.';
  } catch(error) {$('schedule-message').textContent = error.message; if(error.status === 401) signedOut();}
  finally {scheduleBusy = false; $('save-schedule').disabled = false; await refreshSchedules();}
};

async function refreshNotifications() {
  if($('dashboard').hidden) return;
  try {
    const status = await api('/api/notifications');
    if($('dashboard').hidden) return;
    const endpoint = status.server + '/' + status.topic;
    $('notification-topic').href = endpoint; $('notification-topic').textContent = status.topic ? endpoint : 'No topic configured';
    if(!notificationBusy && !notificationSettingDirty) $('notifications-enabled').checked = status.enabled;
    $('test-notification').disabled = notificationBusy || !status.enabled || !status.topic;
    $('notification-status').textContent = `${status.enabled ? 'Enabled' : 'Disabled'} · ${status.pending} pending · ${status.failed} failed deliveries${status.last_sent ? ' · Last accepted by ntfy: ' + runTime(status.last_sent) : ''}${status.last_error ? ' · Last delivery error: ' + status.last_error : ''}${status.storage_error ? ' · Notification storage needs attention' : ''}`;
    notificationsRefreshed = Date.now();
  } catch(error) {$('notification-status').textContent = error.message; if(error.status === 401) signedOut();}
}
$('notification-form').onsubmit = async event => {
  event.preventDefault(); if(notificationBusy) return;
  notificationBusy = true; $('save-notifications').disabled = true; $('test-notification').disabled = true;
  try {
    const status = await api('/api/notifications/settings', {enabled:$('notifications-enabled').checked});
    notificationSettingDirty = false; $('notifications-enabled').checked = status.enabled;
    $('notification-message').textContent = status.enabled ? 'Failure notifications enabled.' : 'Failure notifications disabled. Pending alerts were suppressed.';
  } catch(error) {$('notification-message').textContent = error.message; if(error.status === 401) signedOut();}
  finally {notificationBusy = false; $('save-notifications').disabled = false; await refreshNotifications();}
};
$('notifications-enabled').onchange = () => {notificationSettingDirty = true;};
$('test-notification').onclick = async () => {
  if(notificationBusy) return;
  notificationBusy = true; $('test-notification').disabled = true;
  try {await api('/api/notifications/test', {}); $('notification-message').textContent = 'Test notification queued. Check your ntfy subscription; no Ansible job was started.';}
  catch(error) {$('notification-message').textContent = error.message; if(error.status === 401) signedOut();}
  finally {notificationBusy = false; await refreshNotifications();}
};


async function refreshBackups() {
  if (backupBusy) return;
  backupPreview = null; $('backup-preview').hidden = true;
  try {
    const result = await api('/api/backups');
    $('backup-list').replaceChildren(...result.backups.map(backup => {
      const button = element('button', `${backup.name} · ${runTime(backup.created)} · ${backup.size} bytes`, 'playbook');
      button.onclick = () => previewBackup(backup.id); return button;
    }));
    $('backup-status').textContent = result.backups.length ? 'Select a saved version to preview.' : 'No file backups yet. Backups are created when an existing file is saved.';
  } catch(error) {$('backup-status').textContent = error.message; if(error.status === 401) signedOut();}
}
async function previewBackup(id) {
  if(backupBusy) return;
  backupPreview = null; $('backup-preview').hidden = true;
  try {
    const result = await api('/api/backups/' + encodeURIComponent(id));
    if($('dashboard').hidden) return;
    backupPreview = result;
    $('backup-title').textContent = `${result.name} · ${runTime(result.created)}`;
    $('backup-diff').textContent = result.diff || 'The saved version matches the current file.';
    $('backup-content').textContent = result.content;
    $('backup-preview').hidden = false;
    $('backup-status').textContent = 'Review the changes below. Minus lines are removed; plus lines are restored.';
    await refreshJob();
  } catch(error) {$('backup-status').textContent = error.message; if(error.status === 401) signedOut();}
}
$('refresh-backups').onclick = refreshBackups;
$('restore-backup').onclick = async () => {
  if(backupBusy || editorBusy || inventoryBusy || !backupPreview) return;
  if(dirtyEditor() || dirtyInventory()) {$('backup-status').textContent = 'Save or cancel your editor drafts before restoring a backup.'; return;}
  const preview = backupPreview;
  if(!window.confirm(`Restore ${preview.name} from ${runTime(preview.created)}?\n\nThe current file will be backed up before replacement. Ansible will validate the saved version first.`)) return;
  backupBusy = true; $('restore-backup').disabled = true;
  $('backup-status').textContent = 'Validating and restoring…';
  try {
    const result = await api('/api/backups/restore', {id: preview.id, backup_revision: preview.revision, current_revision: preview.current_revision});
    if(result.name === 'inventory.ini') {inventoryFile = null; validatedInventory = null; $('inventory-panel').hidden = true;}
    else {editingFile = null; viewedFile = null; $('editor-form').hidden = true; await openPlaybook(result.name); selectTab('playbooks');}
    backupPreview = null; $('backup-preview').hidden = true;
    await load(); backupBusy = false; await refreshBackups();
    $('backup-status').textContent = `Restored ${result.name}.${result.backup ? ' Previous file saved as ' + result.backup + '.' : ''}`;
  } catch(error) {
    backupPreview = null; $('restore-backup').disabled = true;
    $('backup-status').textContent = error.message + (error.diagnostics ? '\n' + error.diagnostics : '') + ' Preview the backup again before retrying.';
    if(error.status === 401) signedOut();
  } finally {backupBusy = false; await refreshJob();}
};

$('delete-backup').onclick = async () => {
  if(backupBusy || !backupPreview) return;
  const preview = backupPreview;
  if(!window.confirm(`Permanently delete this backup?\n\n${preview.name} · ${runTime(preview.created)}\n${preview.id}\n\nThe current working file and other backups will remain unchanged. This cannot be undone.`)) return;
  backupBusy = true; $('delete-backup').disabled = true; $('restore-backup').disabled = true;
  $('backup-status').textContent = 'Deleting backup…';
  try {
    await api('/api/backups/remove', {id: preview.id, backup_revision: preview.revision});
    backupPreview = null; $('backup-preview').hidden = true;
    backupBusy = false; await refreshBackups();
    $('backup-status').textContent = `Deleted backup ${preview.id}.`;
  } catch(error) {
    backupPreview = null; $('backup-preview').hidden = true;
    $('backup-status').textContent = error.message + ' Browse backups again before retrying.';
    if(error.status === 401) signedOut();
  } finally {backupBusy = false; await refreshJob();}
};

$('download-backup').onclick = async () => {
  if(backupBusy || !backupPreview) return;
  const preview = backupPreview;
  backupBusy = true; $('download-backup').disabled = true;
  $('backup-status').textContent = 'Preparing download…';
  try {
    const response = await fetch('/api/backup-download/' + encodeURIComponent(preview.id) + '?revision=' + preview.revision);
    if(!response.ok) {
      const error = await response.json();
      throw Object.assign(new Error(error.error || 'Download failed'), {status:response.status});
    }
    const blob = await response.blob();
    if($('dashboard').hidden) return;
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a'); link.href = url; link.download = preview.id;
    document.body.append(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 60000);
    $('backup-status').textContent = `Download requested: ${preview.id}. Check your browser downloads.`;
  } catch(error) {
    $('backup-status').textContent = error.message;
    if(error.status === 401) signedOut();
  } finally {backupBusy = false; await refreshJob();}
};
