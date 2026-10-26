const panelStatus = document.querySelector('#status');
const gameScreen = document.querySelector('#screen');
let busy = false;
let playing = true;

function showError(error) {
  panelStatus.classList.add('error');
  panelStatus.textContent = error.message;
}

async function request(path, payload) {
  const options = payload === undefined ? {} : {
    method: 'POST', headers: {'Content-Type': 'application/json', 'X-Control-Panel': '1'},
    body: JSON.stringify(payload)
  };
  const response = await fetch(path, options);
  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.error || `Request failed (${response.status})`);
  }
  return response;
}

async function updateStatus() {
  const data = await (await request('/api/status')).json();
  playing = data.playing;
  document.querySelector('#rom').textContent = data.rom;
  document.querySelector('#last-command').textContent = data.last_command;
  document.querySelector('#playback').textContent = playing ? 'Pause' : 'Play';
  document.querySelector('#speed').value = String(data.speed);
  document.querySelector('#badge').textContent = data.error ? 'Disconnected' : playing ? `Live · ${data.speed}×` : 'Paused';
  if (data.error) throw new Error(data.error);
  return data;
}

async function run(operation, success) {
  if (busy) return;
  busy = true;
  document.querySelectorAll('button, select').forEach(button => button.disabled = true);
  panelStatus.classList.remove('error');
  panelStatus.textContent = 'Working…';
  try {
    await operation();
    await updateStatus();
    panelStatus.textContent = success;
  } catch (error) {
    showError(error);
  } finally {
    busy = false;
    document.querySelectorAll('button, select').forEach(button => button.disabled = false);
  }
}

function command(path, payload, success = 'Command queued. The live screen will update as it runs.') {
  return run(async () => { await request(path, payload); }, success);
}

function press(buttons) {
  const frames = Number(document.querySelector('#frames').value);
  if (!Number.isInteger(frames) || frames < 1 || frames > 3600) {
    showError(new Error('Enter a whole number from 1 to 3600 frames.'));
    return;
  }
  return command('/api/action', {buttons, frames});
}

document.querySelectorAll('[data-button]').forEach(button => {
  button.addEventListener('click', () => press([button.dataset.button]));
});
document.querySelector('#wait').addEventListener('click', () => press([]));
document.querySelector('#speed').addEventListener('change', event => {
  const speed = Number(event.target.value);
  command('/api/speed', {speed}, `Emulation speed set to ${speed}×.`);
});
document.querySelector('#playback').addEventListener('click', () => {
  command('/api/playback', {playing: !playing}, playing ? 'Paused. Button actions still advance the selected frames.' : 'Live playback resumed.');
});
document.querySelector('#save').addEventListener('click', () => command('/api/save', {}, 'Checkpoint saved.'));
document.querySelector('#restore').addEventListener('click', () => command('/api/load', {}, 'Checkpoint restored.'));
document.querySelector('#capture').addEventListener('click', () => run(async () => {
  const response = await request('/screen');
  const url = URL.createObjectURL(await response.blob());
  const link = document.createElement('a');
  link.href = url;
  link.download = 'gba-screenshot.png';
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}, 'Screenshot saved.'));

gameScreen.onerror = () => {
  showError(new Error('Video stream disconnected. Reconnecting…'));
  setTimeout(() => { gameScreen.src = `/stream?t=${Date.now()}`; }, 2000);
};
gameScreen.src = '/stream';
run(async () => {}, 'Live playback is running. Use Pause to stop the game.');
async function poll() {
  try { await updateStatus(); } catch (error) { showError(error); }
  setTimeout(poll, 500);
}
setTimeout(poll, 500);
