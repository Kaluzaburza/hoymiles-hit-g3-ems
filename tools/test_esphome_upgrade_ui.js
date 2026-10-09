// Offline interaction tests. The browser preview additionally checks real DOM/layout.
const fs = require("node:fs"), vm = require("node:vm"), assert = require("node:assert/strict");
const source = fs.readFileSync("home_assistant/www/hoymiles-update-guide.js", "utf8");
const loaded = vm.runInNewContext(source.replaceAll("export function ", "function ") + "\n({classifyOtaLog,openHoymilesUpdateGuide});", {});
const log = "[I][app:151]: ESPHome version 2026.9.1 compiled on fixture\n[I][esphome.ota:075]: Encryption: required";
for (const [text, expected] of [
  ["INFO ESPHome 2026.9.1", "unknown"],
  ["INFO ESPHome 2026.9.1\n[I][app:151]: ESPHome version 2026.8.2 compiled yesterday", "legacy"],
  [log, "encrypted"], [log.replace("required", "offered, plaintext accepted"), "encrypted"],
  [log.replace("required", "disabled"), "unknown"],
  [log + "\n[I][app:151]: ESPHome version 2026.8.2", "unknown"],
  ["[I][app:151]: ESPHome version 2026.9.0", "unknown"],
  ["\x1b[32m" + log + "\x1b[0m", "encrypted"],
]) assert.equal(loaded.classifyOtaLog(text), expected, text);

class Element {
  constructor(tag = "div") { this.tag = tag; this.listeners = {}; this.children = []; this.value = ""; this.dataset = {}; this.isConnected = true; }
  set innerHTML(html) {
    this.html = html; this.map = new Map();
    for (const name of [...html.matchAll(/(?:id="([^"]+)"|\b(data-[a-z-]+)(?:[\s>]))/g)]) {
      this.map.set(name[1] ? `#${name[1]}` : `[${name[2]}]`, new Element());
    }
  }
  querySelector(key) { return this.map.get(key); }
  setAttribute() {}
  addEventListener(key, handler) { this.listeners[key] = handler; }
  async emit(key, event = {}) { return this.listeners[key]?.(event); }
  async click() { if (!this.disabled) { if (this.onclick) await this.onclick(); return this.emit("click"); } }
  append(node) { this.children.push(node); }
  replaceChildren() { this.children = []; }
  focus() {}
  scrollIntoView() {}
  showModal() {}
  remove() { this.isConnected = false; }
  close() { this.emit("close"); }
}
function fixture({admin = true, reply} = {}) {
  const requests = [], downloads = [], stored = new Map();
  const document = {querySelector: () => null, createElement: (tag) => new Element(tag), body: new Element("body")};
  const functions = vm.runInNewContext(source.replaceAll("export function ", "function ") + "\n({openHoymilesUpdateGuide});", {
    document, Blob, Event, window: {dispatchEvent() {}}, localStorage: {setItem: (...args) => stored.set(...args)},
    URL: {createObjectURL: (blob) => { downloads.push(blob); return "blob:fixture"; }, revokeObjectURL() {}}, setTimeout: (fn) => fn(),
  });
  const hass = {user: {is_admin: admin}, fetchWithAuth: async (url, options) => {
    requests.push({url, ...options});
    return reply ? await reply() : {ok: true, json: async () => ({status:"ready", stage:"final", yaml:"prepared", backup:"original"})};
  }};
  const dialog = functions.openHoymilesUpdateGuide(hass, "pl");
  const input = dialog.querySelector("#hm-update-source"), logs = dialog.querySelector("#hm-update-log"), button = dialog.querySelector("[data-prepare]"), result = dialog.querySelector("[data-result]");
  const fill = async () => { input.value = "substitutions: {name: fixture}"; logs.value = log + "\nLOG_PRIVATE_SENTINEL"; await input.emit("input"); await logs.emit("input"); };
  return {dialog, input, logs, button, result, fill, requests, downloads, stored};
}
(async () => {
  const f = fixture(); await f.fill(); assert.equal(f.button.disabled, false); await f.button.click();
  assert.equal(f.requests.length, 1); assert.equal(f.requests[0].method, "POST");
  assert.equal(f.requests[0].url, "/api/hoymiles_hit_modbus/prepare-esphome-upgrade");
  assert.ok(!f.requests[0].body.includes("LOG_PRIVATE_SENTINEL"), "Raw log stays in browser");
  const [backup, candidate] = f.result.children.filter((node) => node.tag === "button");
  assert.equal(candidate.disabled, true); await candidate.click(); assert.equal(f.downloads.length, 0);
  await backup.click(); assert.equal(await f.downloads[0].text(), "original");
  await candidate.click(); assert.equal(await f.downloads[1].text(), "prepared");
  await f.input.emit("input"); assert.equal(f.result.children.length, 0, "Editing invalidates result");
  f.dialog.close(); assert.equal(f.input.value, ""); assert.equal(f.logs.value, "");

  const nonAdmin = fixture({admin:false}); await nonAdmin.fill(); await nonAdmin.button.click(); assert.equal(nonAdmin.requests.length, 0);
  const denied = fixture({reply:async () => ({ok:false,status:403})}); await denied.fill(); await denied.button.click();
  assert.match(denied.result.children[0].textContent, /administratora/);
  const broken = fixture({reply:async () => { throw new Error("internal secret"); }}); await broken.fill(); await broken.button.click();
  assert.ok(!JSON.stringify(broken.result.children).includes("internal secret"));
  let release;
  const slow = fixture({reply:() => new Promise((resolve) => { release = resolve; })}); await slow.fill();
  const pending = slow.button.click(); await slow.logs.emit("input");
  release({ok:true,json:async () => ({status:"ready",stage:"final",yaml:"stale",backup:"old"})}); await pending;
  assert.equal(slow.result.children.length, 0, "Late response cannot resurrect a stale config");
  const unsafe = fixture(); await unsafe.fill();
  await unsafe.dialog.querySelector("#hm-update-file").emit("change", {target:{files:[{name:"secrets.yaml",size:12,text:async () => "secret"}]}});
  assert.equal(unsafe.input.value, ""); assert.equal(unsafe.button.disabled, true);
  assert.match(unsafe.result.children[0].textContent, /secrets.yaml/);
  const unknown = fixture({reply:async () => ({ok:true,json:async () => ({status:"review_required",code:"<script>bad</script>"})})});
  await unknown.fill(); await unknown.button.click(); assert.equal(unknown.result.children.filter((node) => node.tag === "button").length, 0);
  console.log("PASS: log classification, admin boundary, backup-first, no raw-log upload, stale response, errors and private-file rejection");
})().catch((error) => { console.error(error); process.exitCode = 1; });
