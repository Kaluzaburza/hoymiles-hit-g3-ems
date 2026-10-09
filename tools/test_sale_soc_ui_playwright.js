"use strict";
// Local DOM fixture only. No Home Assistant, device or external HTTP access.
const fs = require("node:fs"), path = require("node:path"), assert = require("node:assert/strict");
const {chromium} = require("playwright");

(async () => {
  const browser = await chromium.launch({headless:true,
    ...(process.env.HOYMILES_TEST_BROWSER ? {executablePath:process.env.HOYMILES_TEST_BROWSER} : {})});
  try {
    const page = await browser.newPage({viewport:{width:1280,height:900}});
    const errors = [];
    page.on("pageerror", error => errors.push(String(error)));
    await page.route("**/*", route => route.abort());
    await page.setContent('<html lang="pl"><meta charset="utf-8"><style>body{background:#080d13;color:#fff;font-family:Arial;margin:16px}ha-card{display:block}</style><body></body></html>');
    await page.evaluate(() => {
      window.customCards = [];
      window.loadCardHelpers = async () => ({createCardElement:() => document.createElement('div')});
    });
    await page.addScriptTag({content:fs.readFileSync(path.join(process.cwd(), 'home_assistant/www/hoymiles-rce-chart-card.js'),'utf8'),type:'module'});
    await page.waitForFunction(() => customElements.get('hoymiles-aurora-variant-a-policy-settings-card'));
    await page.evaluate(() => {
      const stamp = new Date().toISOString();
      const state = (v, attributes={}) => ({state:String(v),attributes,last_reported:stamp,last_updated:stamp});
      window.calls = [];
      window.testHass = {language:'pl',states:{
        'input_boolean.hoymiles_rce_dynamic_soc_enabled':state('on'),
        'sensor.hoymiles_hit_ems_self_use_soc_readback':state(20),
        'input_number.hoymiles_rce_soc_safety_margin':state(60,{min:0,max:90,step:1,unit_of_measurement:'%'}),
      },callService:async (domain, service, data) => window.calls.push({domain,service,data})};
      window.card = document.createElement('hoymiles-aurora-variant-a-policy-settings-card');
      card.setConfig({section:'rce',language:'pl'});
      document.body.append(card);
      card.hass = testHass;
    });
    const input = page.locator('input[data-setting-entity="input_number.hoymiles_rce_soc_safety_margin"]');
    const summary = page.locator('[data-sale-reserve-summary]');
    assert.equal(await input.getAttribute('max'),'90');
    assert.equal(await input.getAttribute('min'),'0');
    assert.match(await summary.textContent(),/20%.*60%.*80%.*maksymalnie 20%/);
    await page.evaluate(() => {
      const reserve = testHass.states['sensor.hoymiles_hit_ems_self_use_soc_readback'];
      delete reserve.last_reported;
      reserve.last_updated = new Date(Date.now()-3600000).toISOString();
      testHass.states['sensor.hoymiles_hit_ems_control_readback_generation'] = {
        state:'123', attributes:{}, last_updated:new Date().toISOString(),
      };
      card.hass={...testHass};
    });
    assert.match(await summary.textContent(),/20%.*60%.*80%.*maksymalnie 20%/);
    await input.fill('90'); await input.dispatchEvent('change');
    const calls = await page.evaluate(() => window.calls);
    assert.equal(calls.length,1);
    assert.deepEqual(calls[0],{domain:'input_number',service:'set_value',data:{entity_id:'input_number.hoymiles_rce_soc_safety_margin',value:90}});
    // Formula follows the acknowledged HA state, not the unacknowledged edit.
    assert.match(await summary.textContent(),/60%.*80%/);
    await page.evaluate(() => {testHass.states['input_number.hoymiles_rce_soc_safety_margin'].state='90';card.hass={...testHass};});
    assert.match(await summary.textContent(),/110%.*ograniczona do 100%.*maksymalnie 0%/);
    await input.fill('91');
    assert.equal(await input.evaluate(el => el.checkValidity()),false);
    await input.blur();
    await page.evaluate(() => {testHass.states['input_number.hoymiles_rce_soc_safety_margin'].state='60';card.hass={...testHass};});
    const output = process.env.HOYMILES_SOC_UI_OUTPUT;
    for (const width of [1280,390]) {
      await page.setViewportSize({width,height:900});
      await summary.scrollIntoViewIfNeeded();
      const bounds = await summary.evaluate(el => ({width:el.getBoundingClientRect().width,scroll:el.scrollWidth,client:el.clientWidth}));
      assert(bounds.width>100 && bounds.scroll<=bounds.client+1,JSON.stringify(bounds));
      if (output) {
        fs.mkdirSync(output,{recursive:true});
        await summary.locator('xpath=..').locator('xpath=..').screenshot({path:path.join(output,`sale-soc-${width}.png`)});
      }
    }
    await page.evaluate(() => {testHass.states['sensor.hoymiles_hit_ems_self_use_soc_readback'].state='unavailable';card.hass={...testHass};});
    assert.match(await summary.textContent(),/Brak aktualnego/);
    assert.deepEqual(errors,[]);
    console.log('PASS real policy-card DOM: 0..90, saved-state formula, cap, missing data, no overflow at 1280/390px; services mocked');
  } finally {await browser.close();}
})().catch(error => {console.error(error);process.exitCode=1;});
