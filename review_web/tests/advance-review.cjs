// Every API call is mocked. No approvals, messages or CRM writes leave this test.
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
require('node:fs').mkdirSync('outputs/advance-review-20260908', { recursive: true });
(async () => {
  const browser = await chromium.launch({ channel: 'chrome', headless: true });
  try {
    const page = await browser.newPage({viewport:{width:1440,height:1000}});
    const now = new Date('2026-09-08T04:00:00Z');
    await page.clock.install({time:now});
    let gets=0, marks=0, navigations=0, patches=0;
    const messages=[72,2,-1,-72].map((hours,i)=>({
      id:`m${i}`,lead_id:`lead${i}`,channel:'email',version:1,review_status:'pending_review',
      created_at:now.toISOString(),updated_at:now.toISOString(),
      crm_snapshot:{company:{name:['未来跟进示例','即将到期示例','已到期示例','严重逾期示例'][i]},contact:{name:'Buyer',email:'buyer@example.com'},sales:{name:'博文 张'},message_route:'conversation_follow_up',review_schedule:{follow_up_at:new Date(+now+hours*3600000).toISOString()}},
      effective_output:{content:{subject:'Follow up',body:`Message ${i}`,body_zh:`中文对照 ${i}`}},recipient_original:'buyer@example.com'
    }));
    await page.route('**/api/**',async route=>{
      const req=route.request(),url=new URL(req.url());
      if(url.pathname==='/api/review/messages'){gets++;return route.fulfill({json:{records:messages}});}
      const m=messages.find(m=>url.pathname.includes(`/messages/${m.id}`));
      if(req.method()==='PATCH'){
        patches++;m.effective_output.content.body=req.postDataJSON().body;m.updated_at=new Date(+now+patches*1000).toISOString();delete m.crm_snapshot.content_review;
        return route.fulfill({json:m});
      }
      if(url.pathname.endsWith('/reviewed')){
        marks++;assert.equal(req.postDataJSON().expected_updated_at,m.updated_at);m.crm_snapshot.content_review={reviewed_by:'review-ui',reviewed_at:new Date().toISOString()};m.updated_at=new Date(+now+10000+marks*1000).toISOString();return route.fulfill({json:m});
      }
      if(url.pathname.endsWith('/approve'))throw new Error('Test must never approve a message');
      return route.fulfill({json:{records:[]}});
    });
    page.on('framenavigated',frame=>{if(frame===page.mainFrame())navigations++;});
    await page.goto(process.env.REVIEW_TEST_URL||'http://127.0.0.1:8000');
    for(const [id,kind] of [['m0','advance_review'],['m1','due_24h'],['m2','overdue'],['m3','overdue_48h']]){
      assert.equal(await page.locator(`[data-message-id="${id}"]`).getAttribute('data-priority-kind'),kind);
    }
    await page.locator('[data-message-id="m0"]').click();
    const approve=page.getByRole('button',{name:'批准并进入 Outbox',exact:true});
    assert.equal(await approve.isDisabled(),true);
    const before=gets;
    await page.getByLabel('客户可见正文').fill('提前编辑的内容');
    await page.getByRole('button',{name:'标记已审阅',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('#detail').textContent.includes('✓ 已审阅 · 待到期'));
    assert.equal(marks,1);assert.equal(patches,1);assert.equal(gets,before);
    await page.locator('[data-message-id="m1"]').click();
    await page.locator('[data-message-id="m0"]').click();
    assert.equal(await page.getByLabel('客户可见正文').inputValue(),'提前编辑的内容');
    await page.screenshot({path:'outputs/advance-review-20260908/ui-advance-review.png',fullPage:true});
    await page.getByLabel('客户可见正文').fill('计时期间保留的未保存内容');
    await page.evaluate(()=>{window.editorBefore=document.querySelector('.review-editor textarea');});
    await page.clock.fastForward(49*3600000);
    assert.equal(await page.locator('[data-message-id="m0"]').getAttribute('data-priority-kind'),'due_24h');
    assert.equal(await approve.isDisabled(),true);
    await page.clock.fastForward(24*3600000);
    assert.equal(await page.locator('[data-message-id="m0"]').getAttribute('data-priority-kind'),'overdue');
    assert.equal(await approve.isDisabled(),false);
    assert.equal(await page.getByLabel('客户可见正文').inputValue(),'计时期间保留的未保存内容');
    assert.ok(await page.evaluate(()=>window.editorBefore===document.querySelector('.review-editor textarea')));
    assert.ok((await page.locator('#detail').innerText()).includes('有未保存修改'));
    assert.equal(gets,before);assert.equal(navigations,1);
    await page.screenshot({path:'outputs/advance-review-20260908/ui-clock-check.png',fullPage:true});
    console.log('PASS: four colors, advance review persisted, future approval disabled, timed transitions, dirty editor retained, no reload or send');
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
