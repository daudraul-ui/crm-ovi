// Chromium UI regression. Fixtures/session are generated separately in an isolated test database.
const {chromium} = require('playwright');
const fs = require('fs');
const fixtures = JSON.parse(fs.readFileSync(process.env.CRM_BROWSER_FIXTURES, 'utf8'));
const base = process.env.CRM_BROWSER_URL || 'http://127.0.0.1:18002';
const assert = (condition, message) => { if (!condition) throw Error(message); };
(async () => {
  const browser = await chromium.launch({executablePath: process.env.CHROMIUM_PATH, headless: true, args: ['--no-sandbox']});
  try {
    for (const [device, viewport] of [['desktop', {width:1440,height:1000}], ['mobile', {width:390,height:844}]]) {
      const context = await browser.newContext({viewport});
      await context.addCookies([{name:'sessionid',value:fixtures.session,url:base,httpOnly:true,sameSite:'Lax'}]);
      const page = await context.newPage();
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      const suffix = fixtures.suffix + '-' + device;
      await page.goto(base + '/');
      for (const kind of ['companies','stores','projects','tasks']) {
        await page.locator(`.metric-link[href="/${kind}/"]`).click();
        assert(new URL(page.url()).pathname === `/${kind}/`, 'Metric card destination');
        await page.goto(base + '/');
      }
      if (device === 'mobile') {
        await page.getByRole('button', {name:'☰'}).click();
        assert(await page.locator('aside').isVisible(), 'Mobile navigation');
        await page.getByRole('button', {name:'☰'}).click();
      }
      await page.goto(base + '/settings/');
      await page.getByRole('link', {name:'+ Добавить сотрудника'}).click();
      await page.locator('[name=username]').fill('employee-' + suffix);
      await page.locator('[name=first_name]').fill('Анна');
      await page.locator('[name=last_name]').fill('Проверка ' + suffix);
      await page.locator('[name=email]').fill('employee-' + suffix + '@example.org');
      await page.locator('[name=password1]').fill('Browser-Employee-Strong-47!');
      await page.locator('[name=password2]').fill('Browser-Employee-Strong-47!');
      await page.getByLabel('Менеджер', {exact:true}).check();
      await page.getByLabel('Инженер', {exact:true}).check();
      await page.getByRole('button', {name:'Сохранить',exact:true}).click();
      await page.getByRole('heading', {name:'Настройки',exact:true}).waitFor();
      assert((await page.locator('article').innerText()).includes('employee-' + suffix), 'Employee list');
      await page.goto(base + '/companies/new/');
      await page.locator('[name=name]').fill('ЮЛ с новым менеджером ' + suffix);
      await page.locator('[name=manager]').selectOption({label:'Анна Проверка ' + suffix + ' (employee-' + suffix + ')'});
      await page.getByRole('button', {name:'Сохранить',exact:true}).click();
      await page.getByRole('heading', {name:'Клиенты',exact:true}).waitFor();
      await page.goto(base + '/chains/new/');
      const chainName = 'ТС UI ' + suffix;
      await page.locator('[name=name]').fill(chainName);
      await page.locator('[name=companies]').selectOption([String(fixtures.company_a),String(fixtures.company_b)]);
      await page.getByRole('button', {name:'Сохранить',exact:true}).click();
      await page.getByRole('link', {name:chainName,exact:true}).click();
      assert((await page.locator('article').innerText()).includes(fixtures.company_b_name), 'Chain legal entities');
      await page.goto(base + '/stores/new/');
      await page.locator('[name=chain]').selectOption({label:chainName});
      await page.waitForFunction(name => [...document.querySelector('[name=company]').options].some(o => o.text === name), fixtures.company_b_name);
      await page.locator('[name=company]').selectOption(String(fixtures.company_b));
      await page.locator('[name=name]').fill('Магазин UI ' + suffix);
      await page.locator('[name=code]').fill('UI-' + suffix);
      await page.locator('[name=address]').fill('Москва, проверка заполненных полей');
      await page.locator('[name=region]').selectOption(String(fixtures.region));
      // Switching networks clears incompatible company/format but preserves other fields.
      await page.locator('[name=chain]').selectOption(String(fixtures.other_chain));
      await page.waitForFunction(name => [...document.querySelector('[name=format]').options].some(o => o.text === name), fixtures.other_format_name);
      assert(await page.locator('[name=company]').inputValue() === '', 'Company reset');
      assert(!(await page.locator('[name=company]').innerText()).includes(fixtures.company_b_name), 'Unrelated company excluded');
      await page.locator('[name=chain]').selectOption({label:chainName});
      await page.waitForFunction(name => [...document.querySelector('[name=company]').options].some(o => o.text === name), fixtures.company_b_name);
      await page.locator('[name=company]').selectOption(String(fixtures.company_b));
      await page.getByRole('button', {name:'+ Новый формат для выбранной сети'}).click();
      await page.locator('#format-name').fill('Формат UI ' + suffix);
      await page.getByRole('button', {name:'Создать формат',exact:true}).click();
      await page.waitForFunction(() => !document.querySelector('#format-dialog').open);
      assert((await page.locator('[name=format] option:checked').innerText()) === 'Формат UI ' + suffix, 'New format selected');
      assert(await page.locator('[name=name]').inputValue() === 'Магазин UI ' + suffix, 'Store name preserved');
      assert(await page.locator('[name=address]').inputValue() === 'Москва, проверка заполненных полей', 'Address preserved');
      assert(await page.locator('[name=region]').inputValue() === String(fixtures.region), 'Region preserved');
      await page.screenshot({path:`work/task0011-${device}-store.png`,fullPage:true});
      await page.getByRole('button', {name:'Сохранить',exact:true}).click();
      await page.getByRole('heading', {name:'Магазины',exact:true}).waitFor();
      await page.getByRole('link', {name:'Магазин UI ' + suffix,exact:true}).click();
      const detail = await page.locator('article').innerText();
      assert(detail.includes(fixtures.company_b_name) && detail.includes('Формат UI ' + suffix), 'Saved store relations');
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), 'No viewport overflow');
      assert(errors.length === 0, 'JS errors: ' + errors.join('; '));
      console.log(`PASS ${device}: employees/roles, manager, chain companies, dependent selectors, global region, inline format/preserved form, metric links`);
      await context.close();
    }
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exit(1); });
