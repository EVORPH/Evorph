const MIN_SECONDS = 15, MAX_SECONDS = 3600;

export function autoUpdateSeconds(configured, { loopback = false, search = '' } = {}) {
  const override = new URLSearchParams(search).get('auto_update_seconds');
  const seconds = loopback && override !== null ? Number(override) : configured;
  return Number.isInteger(seconds) && seconds >= MIN_SECONDS && seconds <= MAX_SECONDS ? seconds : 0;
}

export function createAutoUpdater({ seconds, canRead, read, schedule = setInterval, cancel = clearInterval }) {
  let timer = null;
  function stop() {
    if (timer === null) return;
    cancel(timer); timer = null;
  }
  return {
    get running() { return timer !== null; },
    stop,
    notify() {
      if (!seconds || !canRead()) { stop(); return; }
      if (timer === null) timer = schedule(() => { if (canRead()) read(); }, seconds * 1000);
    },
  };
}
