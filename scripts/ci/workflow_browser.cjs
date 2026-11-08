// Uses only disposable fixtures; exercises business commands through HTML forms.
const {chromium,firefox} = require('playwright');
const fs=require('fs');
const engineName=process.env.CRM_BROWSER_ENGINE || 'chromium';
const fixture=JSON.parse(fs.readFileSync(process.env.CRM_WORKFLOW_FIXTURES,'utf8'))[engineName];
const base=process.env.CRM_BROWSER_URL || 'http://127.0.0.1:18003';
const assert=(v,m)=>{if(!v) throw Error(m);};
(async()=>{
 const browser=await (engineName==='firefox'?firefox:chromium).launch({headless:true});
 const contexts={};
 const pages={};
 const errors=[];
 for(const role of ['manager','engineer','rop','director']){
   contexts[role]=await browser.newContext({viewport:{width:1440,height:1000}});
   await contexts[role].addCookies([{name:'sessionid',value:fixture.sessions[role],url:base,httpOnly:true,sameSite:'Lax'}]);
   pages[role]=await contexts[role].newPage();
   pages[role].on('pageerror',e=>errors.push(e.message));
 }
 async function command(role,kind,id,action,fill){
   const page=pages[role];
   await page.goto(`${base}/workflow/${kind}/${id}/do/${action}/`);
   if(fill) await fill(page);
   await page.getByRole('button',{name:'Сохранить',exact:true}).click();
   await page.waitForURL(`**/workflow/${kind}/${id}/`);
   assert(!await page.locator('.errorlist').count(),'Unexpected validation error');
 }
 try{
   const manager=pages.manager;
   await manager.goto(base+'/workflow/inquiries/new/');
   await manager.locator('#id_name').fill('Обращение из email '+engineName);
   await manager.locator('#id_project').selectOption(String(fixture.project));
   await manager.locator('#id_channel').fill('Email');
   await manager.locator('#id_received_at').fill('2026-11-08T10:00');
   await manager.locator('#id_need').fill('Остекление десяти горок');
   await manager.locator('#id_next_step').fill('Проверить исходные данные');
   await manager.locator('#id_next_at').fill('2027-01-01T10:00');
   await manager.getByRole('button',{name:'Сохранить',exact:true}).click();
   await manager.waitForURL('**/workflow/inquiries/*/');
   assert(!await manager.locator('.errorlist').count(),'Inquiry should be saved');
   await command('manager','specifications',fixture.spec,'approve_manager');
   await command('engineer','specifications',fixture.spec,'approve_engineer');
   await command('rop','quotes',fixture.quote,'policy',async p=>p.locator('#id_minimum_margin').fill('10'));
   await command('manager','quotes',fixture.quote,'submit');
   await command('rop','quotes',fixture.quote,'decide',async p=>{
     await p.locator('#id_role').selectOption('РОП');
     await p.locator('#id_approved').selectOption('yes');
     await p.locator('#id_reason').fill('Цена, маржа и состав проверены');
   });
   await manager.goto(`${base}/workflow/quotes/${fixture.quote}/`);
   assert((await manager.locator('article').innerText()).includes('Согласовано'),'Quote must be approved');
   const downloadPromise=manager.waitForEvent('download');
   await manager.getByRole('link',{name:'Клиентское КП PDF',exact:true}).click();
   const download=await downloadPromise;
   assert((await download.suggestedFilename()).endsWith('.pdf'),'PDF download');
   await command('manager','quotes',fixture.quote,'send',async p=>{
     await p.locator('#id_document').selectOption(String(fixture.document));
     await p.locator('#id_followup_at').fill('2027-01-01T10:00');
   });
   await manager.goto(`${base}/workflow/quotes/${fixture.quote}/do/baseline/`);
   await manager.locator('#id_document').selectOption(String(fixture.document));
   await manager.locator('#id_reason').fill('Заказ письменно утверждён клиентом');
   await manager.getByRole('button',{name:'Сохранить',exact:true}).click();
   await manager.waitForURL('**/workflow/budgets/*/');
   await command('manager','conditions',fixture.condition,'verify',async p=>{
     await p.locator('#id_document').selectOption(String(fixture.document));
     await p.locator('#id_amount').fill('1200000');
     await p.locator('#id_payer').fill('Клиент');
     await p.locator('#id_recipient').fill('OVI');
     await p.locator('#id_order_reference').fill('Заказ 1');
     await p.locator('#id_execution_checked').check();
   });
   await command('manager','orders',fixture.order,'authorize');
   await command('manager','orders',fixture.order,'dispatch',async p=>p.locator('#id_document').selectOption(String(fixture.document)));
   for(const quantity of ['6','4']){
     await command('manager','order-lines',fixture.line,'confirm',async p=>{
       await p.locator('#id_sender').fill('supplier@example.com');
       await p.locator('#id_received_at').fill('2026-11-07T10:00');
       await p.locator('#id_document').selectOption(String(fixture.document));
       await p.locator('#id_quantity').fill(quantity);
       await p.locator('#id_ready_on').fill('2027-01-01');
       await p.locator('#id_composition').fill('Горки: остекление согласованного состава');
       await p.locator('#id_composition_checked').check();
     });
     await manager.goto(`${base}/workflow/orders/${fixture.order}/`);
     const text=await manager.locator('article').innerText();
     assert(text.includes(quantity==='6'?'Частично подтверждён':'Подтверждён поставщиком'),'Partial/full confirmation');
   }
   for(const viewport of [{width:1440,height:1000},{width:390,height:844}]){
     await manager.setViewportSize(viewport);
     await manager.goto(`${base}/workflow/quotes/${fixture.quote}/`);
     assert(await manager.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'No horizontal viewport overflow');
     await manager.screenshot({path:`work/workflow-${engineName}-${viewport.width}.png`,fullPage:true});
   }
   const denied=await pages.engineer.goto(`${base}/workflow/quotes/${fixture.quote}/`);
   assert(denied.status()===403,'Engineer cannot read quote economics');
   assert(errors.length===0,errors.join('\n'));
   console.log('PASS workflow '+engineName+': inquiry, technical approvals, quote, PDF, dispatch, baseline, payment, launch, partial/full supplier confirmations, desktop/mobile, permissions');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
