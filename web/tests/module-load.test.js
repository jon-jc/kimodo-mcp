import test from 'node:test';
import assert from 'node:assert/strict';
import { loadStudioModule, moduleLoadMessage } from '../src/module-load.js';

test('stale JavaScript and CSS imports explain manual recovery without discarding the current draft', async () => {
  for (const message of ['Failed to fetch dynamically imported module: /assets/panel-old.js', 'Unable to preload CSS for /assets/old.css', 'error loading dynamically imported module', 'Importing a module script failed.']) {
    let calls = 0; const cause = new TypeError(message);
    await assert.rejects(() => loadStudioModule(async () => { calls++; throw cause; }), error => {
      assert.match(error.message, /draft is still here/); assert.match(error.message, /Save or copy it, then refresh/);
      assert.equal(error.cause, cause); return true;
    });
    assert.equal(calls, 1);
  }
});

test('ordinary network and runtime errors retain their specific diagnosis', async () => {
  for (const message of ['Failed to fetch', 'Invalid character handoff', 'Cannot read properties of undefined']) {
    const error = new Error(message); assert.equal(moduleLoadMessage(message), message);
    await assert.rejects(() => loadStudioModule(async () => { throw error; }), failure => failure === error);
  }
  const loaded = { panel: true }; assert.equal(await loadStudioModule(async () => loaded), loaded);
});
