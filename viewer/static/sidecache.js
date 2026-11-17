// The side tabs' data cache (Cats, Blockers, Graphs). renderSide runs on every matrix hover, so a
// tab asks for its data again and again while an answer is on its way, and each ask queues behind
// the solver. One ask per (slot, key) at a time; an answer that comes back after invalidate() (a
// solve ending, a lock: the refresh renders again and asks anew), or after a newer ask for another
// key in the same slot overtook it (the Cats tab's player flipped), is dropped, not cached.
// No DOM, so viewer/test_sidecache_js.mjs can check it.

export function sideCache(fetchJSON, onError) {
  const cache = {}, inflight = {};
  let gen = 0;
  return {
    /** Forget every slot's data; answers already on their way are dropped. */
    invalidate() {
      gen++;
      for (const k of Object.keys(cache)) delete cache[k];
    },
    /** The slot's entry for `key` (build(answer) the first time), or null when there is nothing to
     *  show yet: the ask already in flight renders it, or the answer went stale. */
    async load(slot, key, url, build) {
      if (cache[slot] && cache[slot].key === key) return cache[slot];
      const f = inflight[slot];
      if (f && f.key === key && f.gen === gen) return null;
      const mine = { key, gen };
      inflight[slot] = mine;
      try {
        const data = await fetchJSON(url);
        if (mine.gen !== gen || inflight[slot] !== mine) return null;
        cache[slot] = { ...build(data), key };
        return cache[slot];
      } catch (e) {
        if (mine.gen === gen && inflight[slot] === mine) onError(e);
        return null;
      } finally {
        if (inflight[slot] === mine) inflight[slot] = null;
      }
    },
  };
}
