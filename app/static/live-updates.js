/* One in-flight read per view. Disposed views never apply late responses. */
export function watchResource({load, update, error = () => {}, interval = 3000, initial, visibility = document}) {
  let stopped = false, timer, reading = false;
  let previous = JSON.stringify(initial);
  const schedule = () => { if (!stopped) timer = setTimeout(tick, interval); };
  async function tick() {
    if (stopped || reading) return;
    clearTimeout(timer);
    if (visibility.hidden) { schedule(); return; }
    reading = true;
    try {
      const value = await load();
      if (stopped) return;
      const next = JSON.stringify(value);
      if (next !== previous) { update(value); previous = next; }
      if (!stopped) error(null);
    } catch (cause) { if (!stopped) error(cause); }
    finally { reading = false; schedule(); }
  }
  const resume = () => { if (!visibility.hidden) tick(); };
  visibility.addEventListener('visibilitychange', resume);
  schedule();
  return () => { stopped = true; clearTimeout(timer); visibility.removeEventListener('visibilitychange', resume); };
}
