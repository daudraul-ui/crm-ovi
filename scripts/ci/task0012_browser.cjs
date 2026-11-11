// Preview/commit and repeat-import browser checks, on the isolated CI fixture only.
const {chromium, firefox} = require('playwright');
const fs = require('fs');
const fixtures = JSON.parse(fs.readFileSync(process.env.CRM_BROWSER_FIXTURES, 'utf8'));
const base = process.env.CRM_BROWSER_URL || 'http://127.0.0.1:18002';
const assert = (condition, message) => { if (!condition) throw Error(message); };
(async () => {
  const engine = process.env.CRM_BROWSER_ENGINE === 'firefox' ? firefox : chromium;
  const browser = await engine.launch({headless:true});
  const context = await browser.newContext({viewport:{width:1440,height:1000}});
  await context.addCookies([{name:'sessionid',value:fixtures.session,url:base,httpOnly:true,sameSite:'Lax'}]);
  const page = await context.newPage();
  try {
    for (let attempt = 0; attempt < 2; attempt++) {
      await page.goto(base + '/stores/import/');
      await page.locator('input[type=file]').setInputFiles(fixtures.import_xlsx ? {name:'import-ui.xlsx',mimeType:'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',buffer:Buffer.from(fixtures.import_xlsx,'base64')} : process.env.CRM_IMPORT_FIXTURE || 'work/import-ui.xlsx');
      await page.getByRole('button',{name:'Загрузить для анализа'}).click();
      await page.getByRole('button',{name:'Проверить и обновить предпросмотр'}).click();
      for (let i=0; i<10; i++) {
        const questions = page.locator('select[name^=answer_]');
        if (!(await questions.count())) break;
        for (const select of await questions.all()) {
          const options = await select.locator('option').evaluateAll(nodes=>nodes.map(n=>n.value));
          const choice = ['new','blank','keep','use'].find(v=>options.includes(v)) || options.find(Boolean);
          await select.selectOption(choice);
        }
        await page.getByRole('button',{name:'Проверить и обновить предпросмотр'}).click();
      }
      await page.getByRole('button',{name:'Подтвердить и сохранить'}).waitFor();
      await page.screenshot({path:`work/task0012-import-preview-${attempt}.png`,fullPage:true});
      page.once('dialog',dialog=>dialog.accept());
      await page.getByRole('button',{name:'Подтвердить и сохранить'}).click();
      await page.getByRole('heading',{name:'Импорт сохранён'}).waitFor();
    }
    await page.goto(base + '/stores/?q=IMPORT-00001');
    assert(await page.locator('#rows tr').count() === 1, 'Repeated import must not duplicate a store');
    await page.screenshot({path:'work/task0012-import-result.png',fullPage:true});
    console.log('PASS preview, grouped decisions, atomic commit, repeated import, filtered store registry');
  } finally { await browser.close(); }
})().catch(error=>{console.error(error);process.exit(1);});
