// The side tabs' data cache (Cats, Blockers, Graphs). renderSide runs on every matrix hover, so a
// tab asks for its data again and again while an answer is on its way, and each ask queues behind
// the solver. One ask per (slot, key) at a time, its answer shared by every call that wanted it; an
// answer that comes back after invalidate() (a
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
    /** The slot's entry for `key` (build(answer) the first time), or null when the answer went stale
     *  or failed. A call for a key already in flight shares that ask's answer rather than asking
     *  again; every caller then runs its own checks, so the latest one draws (a caller that
     *  navigated away drops it, and one that asked with the same key after it still draws). */
    load(slot, key, url, build) {
      if (cache[slot] && cache[slot].key === key) return Promise.resolve(cache[slot]);
      const f = inflight[slot];
      if (f && f.key === key && f.gen === gen) return f.promise;
      const mine = { key, gen };
      inflight[slot] = mine;
      mine.promise = (async () => {
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
      })();
      return mine.promise;
    },
  };
}
