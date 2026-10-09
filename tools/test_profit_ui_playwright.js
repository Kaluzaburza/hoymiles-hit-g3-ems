"use strict";
// Isolated browser fixture, never a live HA or inverter. Export screenshots on request.
const assert=require("node:assert/strict"),fs=require("node:fs"),path=require("node:path");
const {chromium}=require("playwright");
const source=fs.readFileSync(path.join(__dirname,"../home_assistant/www/hoymiles-rce-chart-card.js"),"utf8");
async function main(){
  let browser;try{browser=await chromium.launch({headless:true,channel:"chrome"});}catch(_){browser=await chromium.launch({headless:true});}
  try{
    const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[];
    page.on("pageerror",error=>errors.push(String(error)));
    await page.setContent('<html><body style="margin:0;background:#08121e"><div id="stage"></div></body></html>');
    await page.evaluate(()=>{window.customCards=[];});
    await page.addScriptTag({content:source,type:"module"});
    await page.waitForFunction(()=>!!customElements.get("hoymiles-aurora-profit-card"));
    assert.deepEqual(errors,[],"asset load");
    await page.evaluate(()=>{
      const common={covered_seconds:3600,buy_covered_seconds:3600,sell_covered_seconds:3600,model_seconds:0,
        import_kwh:1,export_kwh:3,priced_import_kwh:1,priced_export_kwh:3,import_cost:.5,export_revenue:1.5,cash_balance:1,estimated_benefit:null};
      window.fixture={schema:1,period:"month",start:"2026-10-01",end:"2026-11-01",timezone:"Europe/Warsaw",started_at:1791252000,
        manual_price_basis:"Net",archive_bytes:100000,tariff_count:48,
        totals:{...common,coverage_percent:87.5,import_kwh:10,export_kwh:24,import_cost:8,export_revenue:12,cash_balance:4,
          priced_import_kwh:9.9,priced_export_kwh:24,benefit_status:"partial",model_coverage_percent:6.67,
          estimated_benefit:1.25,model_seconds:240,model_cash_delta:3,inventory_delta:-1.75,unpriced_import_kwh:.1,unpriced_export_kwh:0},
        purchases:[{source:"official:PGE:G12:2026.1",zone:"low",energy_kwh:8,priced_kwh:8,amount_pln:5,average_price:.625,unpriced_kwh:0},
          {source:"official:PGE:G12:2026.1",zone:"peak",energy_kwh:1.9,priced_kwh:1.9,amount_pln:3,average_price:3/1.9,unpriced_kwh:0},
          {source:"unavailable",zone:"unknown",energy_kwh:.1,priced_kwh:0,amount_pln:null,average_price:null,unpriced_kwh:.1}],
        sales:[{source:"pstryk",zone:"all",energy_kwh:20,priced_kwh:20,amount_pln:10,average_price:.5,unpriced_kwh:0},
          {source:"pse_rce",zone:"all",energy_kwh:4,priced_kwh:4,amount_pln:2,average_price:.5,unpriced_kwh:0}],
        categories:[{...common,category:"self_use"},{...common,category:"pv_delay"},{...common,category:"tariff_support"}],
        series:[{...common,key:"2026-10-05"},{...common,key:"2026-10-06",covered_seconds:0,cash_balance:null},
          {...common,key:"2026-10-07",export_revenue:-.5,cash_balance:-1}],
        tariffs:Array.from({length:24},(_,i)=>({...common,local_hour:i,utc_offset_minutes:120,buy_source:"PGE G12w",buy_zone:"low",buy_net:.35,
          sell_source:"pstryk",sell_net:-.02,first_day:"2026-10-06",last_day:"2026-10-06"}))};
      window.calls=[];window.services=[];window.requests=[];
      window.hass={config:{time_zone:"Europe/Warsaw"},locale:{language:"pl"},states:{},
        callApi:async(method,url)=>{calls.push({method,url});const copy=structuredClone(fixture);if(url.includes("period=day"))copy.series.forEach((r,i)=>r.key=String(1791252000+i*3600));return copy;},
        callService:async(...args)=>services.push(args)};
      window.card=document.createElement("hoymiles-aurora-profit-card");
      card.setConfig({});card.hass=hass;document.querySelector("#stage").append(card);
    });
    await page.locator("hoymiles-aurora-profit-card h1").waitFor();
    await page.waitForFunction(()=>card._data);
    // Compare actual computed page framing with Energia at each viewport.
    async function checkAuroraFrame(){
      const metrics=await page.evaluate(async()=>{
        window.loadCardHelpers=async()=>({createCardElement:()=>document.createElement("div")});
        const reference=document.createElement("hoymiles-aurora-compact-page-card");
        reference.setConfig({page:"energy"});reference.hass=hass;
        document.querySelector("#stage").append(reference);
        await new Promise(resolve=>requestAnimationFrame(resolve));
        const read=element=>{
          const root=element.shadowRoot.querySelector(".root"),page=element.shadowRoot.querySelector(".page");
          const heading=element.shadowRoot.querySelector("h1"),eyebrow=element.shadowRoot.querySelector(".eyebrow");
          const rootStyle=getComputedStyle(root),pageStyle=getComputedStyle(page),headingStyle=getComputedStyle(heading),eyebrowStyle=getComputedStyle(eyebrow);
          return {background:rootStyle.backgroundImage,padding:pageStyle.padding,maxWidth:pageStyle.maxWidth,
            heading:[headingStyle.fontSize,headingStyle.fontFamily,headingStyle.lineHeight,headingStyle.letterSpacing],
            eyebrow:[eyebrowStyle.color,eyebrowStyle.fontSize,eyebrowStyle.fontWeight],
            top:eyebrow.getBoundingClientRect().top-root.getBoundingClientRect().top,
            border:pageStyle.borderTopWidth,radius:pageStyle.borderTopLeftRadius};
        };
        const result={profit:read(card),energy:read(reference)};reference.remove();return result;
      });
      assert.deepEqual(metrics.profit,metrics.energy,"Earnings must share Energia's background, header and page spacing");
    }
    await checkAuroraFrame();
    assert.match(await page.locator("hoymiles-aurora-profit-card .badge").innerText(),/Ceny netto/);
    assert.equal(await page.locator(".flow-panel").count(),2,"separate purchase and sale panels");
    assert.match(await page.evaluate(()=>calls[0].url),/period=month/);
    assert.equal(await page.locator("details[open]").count(),0,"details start collapsed");
    assert.doesNotMatch(await page.locator("[data-body]").innerText(),/Saldo|1,25 zł/);
    assert.match(await page.locator(".purchase [data-zone=low]").innerText(),/Tania strefa[\s\S]*8,00[\s\S]*5,00 zł/);
    assert.match(await page.locator(".purchase [data-zone=peak]").innerText(),/Droga strefa[\s\S]*1,90[\s\S]*3,00 zł/);
    assert.match(await page.locator(".purchase .average").innerText(),/0,808 zł\/kWh/,"weighted average excludes unpriced kWh");
    assert.match(await page.locator(".purchase [data-zone=unknown]").innerText(),/— zł/);
    assert.match(await page.locator(".sale").innerText(),/Pstryk/);
    assert.match(await page.locator(".sale").innerText(),/RCE \(PSE\)/);
    assert.equal(await page.evaluate(()=>services.length),0);
    assert.equal(await page.locator("svg rect[data-flow]").count(),4,"gap has no bars");
    assert.equal(await page.locator("svg path").count(),0,"no balance series");
    assert.match(await page.locator('svg rect[data-flow="sale"] title').last().textContent(),/-0,50 zł/,"negative price retained");
    const beforeUnitToggle=await page.evaluate(()=>calls.length);
    await page.locator('[data-unit="energy"]').click();
    assert.match(await page.locator('svg rect[data-flow="sale"] title').last().textContent(),/3,00 kWh/);
    assert.equal(await page.evaluate(()=>calls.length),beforeUnitToggle,"unit toggle does not query HA");
    await page.locator('[data-unit="money"]').click();
    await page.locator('[data-detail="model"] summary').click();
    assert.match(await page.locator('[data-detail="model"]').innerText(),/4 min[\s\S]*6,67%[\s\S]*tylko dla dostępnych fragmentów[\s\S]*1,25 zł/);
    await page.locator('[data-detail="model"] summary').click();
    await page.getByRole("button",{name:"Tydzień",exact:true}).click();
    assert.match(await page.evaluate(()=>calls.at(-1).url),/period=week/);
    await page.locator('[data-detail="prices"] summary').click();
    const groupText=await page.locator('.flows').innerText();
    await page.locator('[data-page="1"]').click();
    assert.match(await page.evaluate(()=>calls.at(-1).url),/tariff_offset=24/);
    assert.equal(await page.locator('.flows').innerText(),groupText,"pagination never changes period totals");
    assert.equal(await page.locator('[data-detail="prices"]').getAttribute('open'),"","pagination keeps list open");
    await page.locator('[data-detail="method"] summary').click();
    await page.locator("[data-manual-basis]").selectOption("Net");
    assert.deepEqual(await page.evaluate(()=>services.at(-1)),["input_select","select_option",{entity_id:"input_select.hoymiles_profit_manual_price_basis",option:"Net"}]);
    await page.getByRole("button",{name:"Dzień",exact:true}).click();
    assert.equal(await page.locator("td").nth(1).evaluate(node=>getComputedStyle(node).color),"rgb(219, 234, 247)");
    await page.locator('[data-detail="prices"] summary').click();
    await page.locator('[data-detail="method"] summary').click();
    await page.getByRole("button",{name:"Miesiąc",exact:true}).click();
    if(process.env.HOYMILES_PROFIT_SCREENSHOTS){fs.mkdirSync(process.env.HOYMILES_PROFIT_SCREENSHOTS,{recursive:true});await page.screenshot({path:path.join(process.env.HOYMILES_PROFIT_SCREENSHOTS,"desktop.png"),fullPage:true});}
    await page.setViewportSize({width:390,height:844});
    await checkAuroraFrame();
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),"page must not scroll horizontally on mobile");
    if(process.env.HOYMILES_PROFIT_SCREENSHOTS)await page.screenshot({path:path.join(process.env.HOYMILES_PROFIT_SCREENSHOTS,"mobile.png"),fullPage:true});
    // Out-of-order responses cannot replace the newly selected period.
    await page.evaluate(()=>{hass.callApi=(method,url)=>new Promise(resolve=>requests.push({url,resolve}));});
    await page.getByRole("button",{name:"Miesiąc",exact:true}).click();
    await page.getByRole("button",{name:"Rok",exact:true}).click();
    await page.evaluate(()=>{requests[1].resolve({...fixture,start:"2026-01-01",end:"2027-01-01"});});
    await page.evaluate(()=>{requests[0].resolve({...fixture,start:"2026-10-01"});});
    assert.match(await page.locator(".period-heading").innerText(),/2026-01-01/);
    // Missing archive is not a display of zero earnings.
    await page.evaluate(()=>{hass.callApi=async()=>{throw Error("offline");};});
    await page.getByRole("button",{name:"Odśwież",exact:true}).click();
    await page.getByText("Archiwum jest niedostępne.",{exact:false}).waitFor();
    assert.equal(await page.locator(".flow-panel").count(),0);
    // Empty archive + English language path.
    await page.evaluate(()=>{
      card.remove();hass.locale.language="en";
      hass.callApi=async()=>({...fixture,totals:{...fixture.totals,covered_seconds:0,cash_balance:null,estimated_benefit:null,coverage_percent:0,model_seconds:0,benefit_status:"unavailable"},series:[],categories:[],tariffs:[],purchases:[],sales:[],tariff_count:0});
      card=document.createElement("hoymiles-aurora-profit-card");card.setConfig({});card.hass=hass;document.querySelector("#stage").append(card);
    });
    await page.getByRole("heading",{name:"Earnings",exact:true}).waitFor();
    await page.getByText("No records in this period.",{exact:true}).waitFor();
    assert.equal(await page.locator(".flow-totals strong").first().innerText(),"— kWh");
    assert.equal(await page.locator(".flow-totals strong").nth(1).innerText(),"— zł");
    assert.deepEqual(errors,[]);
    console.log("PASS earnings browser: PL/EN, desktop/mobile, monthly buy/sell and zones, weighted price, partial EMS evidence, chart gaps/negative prices, pagination, request race, empty/error, user-only setting");
  }finally{await browser.close();}
}
main().catch(err=>{console.error(err);process.exitCode=1;});
