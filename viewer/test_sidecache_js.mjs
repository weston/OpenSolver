// Unit test for the side tabs' data cache (viewer/static/sidecache.js).
// usage: node viewer/test_sidecache_js.mjs
import { sideCache } from "./static/sidecache.js";

let fails = 0;
const check = (cond, msg) => { console.log((cond ? "  ok: " : "  FAIL: ") + msg); if (!cond) fails++; };

// a fake server: every ask is recorded and answered only when the test says so
function fakeServer() {
  const asks = [];
  const fetchJSON = (url) => new Promise((resolve, reject) => asks.push({ url, resolve, reject }));
  return { asks, fetchJSON };
}
const tick = () => new Promise((r) => setTimeout(r, 0));
const build = (data) => ({ data });

async function main() {
  // hovers while the first answer is on its way ask nothing more; the first call gets the data
  {
    const srv = fakeServer(), errs = [];
    const c = sideCache(srv.fetchJSON, (e) => errs.push(e));
    const first = c.load("gr", "r:0", "/api/graphs?id=r:0", build);
    const hovers = [c.load("gr", "r:0", "/api/graphs?id=r:0", build), c.load("gr", "r:0", "/api/graphs?id=r:0", build)];
    check(srv.asks.length === 1, `one ask while it is in flight (got ${srv.asks.length})`);
    srv.asks[0].resolve({ v: 1 });
    const e = await first;
    check(e && e.data.v === 1 && e.key === "r:0", "the first call gets the answer");
    // each caller gets it and runs its own checks: one that navigated away drops it, a later one with
    // the same key (a deal under the same decision) still draws (#457 review)
    check((await Promise.all(hovers)).every((x) => x === e), "the calls made meanwhile share the same answer");
    check((await c.load("gr", "r:0", "/x", build)) === e && srv.asks.length === 1, "later calls are served from the cache");
  }
  // an answer that comes back after invalidate() is dropped, and the next call asks again
  {
    const srv = fakeServer();
    const c = sideCache(srv.fetchJSON, () => {});
    const p = c.load("blk", "r:0", "/a", build);
    c.invalidate();
    srv.asks[0].resolve({ v: "old" });
    check((await p) === null, "a stale answer is not returned");
    const q = c.load("blk", "r:0", "/a", build);
    check(srv.asks.length === 2, "after a refresh the same key is asked again");
    srv.asks[1].resolve({ v: "new" });
    check((await q).data.v === "new", "and the fresh answer is used");
  }
  // a newer ask for another key overtakes an older one: the older answer is dropped, even if it is last
  {
    const srv = fakeServer();
    const c = sideCache(srv.fetchJSON, () => {});
    const oop = c.load("cats", "r:0|OOP", "/oop", build);
    const ip = c.load("cats", "r:0|IP", "/ip", build);
    srv.asks[1].resolve({ v: "IP" });
    check((await ip).data.v === "IP", "the newer key's answer is used");
    srv.asks[0].resolve({ v: "OOP" });
    check((await oop) === null, "the overtaken older answer is dropped");
    check((await c.load("cats", "r:0|IP", "/ip", build)).data.v === "IP", "and does not replace the cache");
  }
  // slots are independent; an error is reported once, by the call that asked, unless stale
  {
    const srv = fakeServer(), errs = [];
    const c = sideCache(srv.fetchJSON, (e) => errs.push(e));
    const a = c.load("gr", "k", "/g", build), b = c.load("blk", "k", "/b", build);
    check(srv.asks.length === 2, "two slots, two asks");
    srv.asks[0].reject(new Error("boom"));
    check((await a) === null && errs.length === 1, "an error is reported");
    c.invalidate();
    srv.asks[1].reject(new Error("late"));
    check((await b) === null && errs.length === 1, "a stale error is not");
    c.load("gr", "k", "/g", build);
    check(srv.asks.length === 3, "after an error the key can be asked again");
  }
  await tick();
}

await main();
if (fails) { console.log(`${fails} FAILED`); process.exit(1); }
console.log("ALL PASSED");
