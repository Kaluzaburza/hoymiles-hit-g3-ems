const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const read = (path) => fs.readFileSync(path, 'utf8');
const canonical = read('home_assistant/www/hoymiles-rce-chart-card.js');
const bootstrap = read('home_assistant/www/hoymiles-dashboard-strategy.js');
const assets = read('custom_components/hoymiles_hit_modbus/assets.py');
const revision = Number(assets.match(/FRONTEND_ASSET_REVISION = (\d+)/)?.[1]);
const element = 'll-strategy-dashboard-hoymiles-hit-xxl-g3';
const origin = 'https://ha.example';
const bundle = `${origin}/local/hoymiles-rce-chart-card.js?v=1.5.8.${revision}&history=48h-executed`;
const prefix = canonical.slice(0, canonical.indexOf('const HOYMILES_AURORA_ACCENTS'));
assert.ok(prefix.includes('customElements.define'));

function fixture() {
  const registry = new Map();
  const imports = [];
  const requests = [];
  const sandbox = {
    URL,
    HTMLElement: class {},
    window: {location: {origin}},
    document: {currentScript: null, scripts: []},
    customElements: {
      get: (name) => registry.get(name),
      define(name, constructor) {
        assert.equal(registry.has(name), false, 'custom element must never be redefined');
        registry.set(name, constructor);
      },
    },
    fetch: async (url, options) => {
      requests.push({url: url.href, options});
      return {ok: true, json: async () => ({title: 'fixture', views: []})};
    },
    hoymilesDecorateDashboard: (dashboard, language) => ({...dashboard, language}),
  };
  const loadCanonical = (source = prefix) => vm.runInNewContext(
    `(function () { ${source.replaceAll('import.meta.url', JSON.stringify(bundle))} })();`, sandbox,
  );
  sandbox.importCanonical = async (url) => { imports.push(url); loadCanonical(); };
  const loadBootstrap = (source = bootstrap) => vm.runInNewContext(
    source.replace('import(canonicalModuleUrl.href)', 'importCanonical(canonicalModuleUrl.href)'), sandbox,
  );
  return {registry, imports, requests, loadCanonical, loadBootstrap};
}

(async () => {
  // Exercise actual registration and generate paths before the simple version
  // assertions: revision 103 bootstrap + revision 102 canonical reproduced the
  // production failure only when the bootstrap claimed the element first.
  for (const order of ['bootstrap-first', 'canonical-first', 'old-bootstrap-first', 'old-canonical-first', 'late-old-bootstrap']) {
    const f = fixture();
    const staleBootstrap = bootstrap.replace(/const frontendRevision = \d+;/, `const frontendRevision = ${revision - 1};`);
    const staleCanonical = prefix.replace(/static hoymilesFrontendRevision = \d+;/, `static hoymilesFrontendRevision = ${revision - 1};`);
    if (order === 'old-bootstrap-first') f.loadBootstrap(staleBootstrap);
    if (order === 'old-canonical-first') f.loadCanonical(staleCanonical);
    if (order === 'canonical-first' || order === 'late-old-bootstrap') f.loadCanonical();
    const original = f.registry.get(element);
    f.loadBootstrap();
    if (order === 'late-old-bootstrap') f.loadBootstrap(staleBootstrap);
    const registered = f.registry.get(element);
    if (original) assert.equal(registered, original, `${order}: preserve constructor`);
    const dashboard = await registered.generate({title: 'Host'}, {locale: {language: 'pl'}});
    assert.equal(dashboard.title, 'Host', order);
    assert.equal(dashboard.language, 'pl', order);
    assert.equal(registered.hoymilesFrontendRevision, revision, order);
    assert.equal(registered.hoymilesCanonicalModule, true, order);
    assert.ok(f.imports.every((url) => url === bundle), order);
    assert.equal(f.requests[0].url, `${origin}/local/dashboard_hoymiles_pl.json`, order);
    assert.equal(f.requests[0].options.cache, 'no-store', order);
    await registered.generate({}, {language: 'en'});
    assert.ok(f.imports.length <= 1, `${order}: no duplicate import`);
  }
  assert.equal(Number(canonical.match(/static hoymilesFrontendRevision = (\d+);/)?.[1]), revision);
  assert.equal(Number(bootstrap.match(/const frontendRevision = (\d+);/)?.[1]), revision);
  for (const name of ['hoymiles-rce-chart-card.js', 'hoymiles-dashboard-strategy.js']) {
    assert.equal(read(`custom_components/hoymiles_hit_modbus/resources/www/${name}`), read(`home_assistant/www/${name}`));
  }
  console.log(`PASS frontend revision ${revision}: five real bootstrap/module orders, PL/EN, cache and generated parity`);
})().catch((error) => { console.error(error); process.exitCode = 1; });
