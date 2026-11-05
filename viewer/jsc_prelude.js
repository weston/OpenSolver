// The two Node globals the viewer's JS unit tests use, for macOS's JavaScriptCore shell (jsc),
// so `make test` runs them on a Mac without Node:  jsc viewer/jsc_prelude.js -m viewer/test_x.mjs
// A test's process.exit(nonzero) throws, and jsc then exits nonzero, which fails the make target.
globalThis.console = { log: (...a) => print(a.join(" ")), error: (...a) => print(a.join(" ")) };
globalThis.process = { exit: (code) => { if (code) throw new Error("exit " + code); } };
