import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';
import assert from 'node:assert/strict';

const html = readFileSync(new URL('../src/optionsagent/web/index.html', import.meta.url), 'utf8');
const auth = html.split('<script>')[1].split('const $=')[0];
const request = 'async function request(' + html.split('async function request(')[1].split('async function control(')[0];
function storage() {
  const values = new Map();
  return {getItem: key => values.get(key), setItem: (key, value) => values.set(key, value)};
}
function page(hash, local, session, fetch) {
  const context = {location: {hash, origin: 'http://127.0.0.1:8766'}, localStorage: local,
    sessionStorage: session, window: {addEventListener() {}}, fetch, refresh() {}};
  vm.runInNewContext(auth + request + ';globalThis.client={request,authenticate};', context);
  return context.client;
}
const goodResponse = {ok: true, status: 200, json: async () => ({paper: true})};

test('restored private URL works after all browser storage is lost', async () => {
  const fetch = async (_, options) => {
    assert.equal(options.headers.Authorization, 'Bearer private-key');
    return goodResponse;
  };
  await page('#private-key', storage(), storage(), fetch).request('/api/state');
  await page('#private-key', storage(), storage(), fetch).request('/api/state');
});

test('bare IP survives tab storage loss via persistent local access', async () => {
  const local = storage();
  const fetch = async (_, options) => {
    assert.equal(options.headers.Authorization, 'Bearer private-key');
    return goodResponse;
  };
  await page('#private-key', local, storage(), fetch).request('/api/state');
  await page('', local, storage(), fetch).request('/api/state');
});

test('cookie access works with no key or browser storage', async () => {
  const unavailable = {getItem() {throw Error('unavailable');}, setItem() {throw Error('unavailable');}};
  const fetch = async (_, options) => {
    assert.equal(options.credentials, 'same-origin');
    assert.equal(options.headers.Authorization, undefined);
    return goodResponse;
  };
  assert.equal((await page('', unavailable, unavailable, fetch).request('/api/state')).paper, true);
});

test('403 attempts session recovery once and retries the original request', async () => {
  const paths = [];
  const fetch = async path => {
    paths.push(path);
    return paths.length === 1 ? {ok: false, status: 403} : goodResponse;
  };
  await page('#private-key', storage(), storage(), fetch).request('/api/state');
  assert.deepEqual(paths, ['/api/state', '/api/session', '/api/state']);
});
