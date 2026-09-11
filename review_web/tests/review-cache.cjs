// Run against the built Review UI. All API calls are intercepted; no CRM writes or sends.
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
(async () => {
  const browser = await chromium.launch({ channel: 'chrome', headless: true });
  try {
    const page = await browser.newPage();
    let gets = 0, failReject = false, navigations = 0;
    const messages = [1, 2, 3].map(i => ({
      id: `m${i}`, lead_id: `l${i}`, channel: 'linkedin', version: 1,
      created_at: `2026-09-0${i}T00:00:00Z`, updated_at: 'v1', review_status: 'pending_review',
      crm_snapshot: { company: { name: `Company ${i}` }, contact: { name: `Contact ${i}` } },
      effective_output: { content: { body: `Draft ${i}`, body_zh: `草稿 ${i}` } },
    }));
    await page.route('**/api/**', async route => {
      const r = route.request(), url = new URL(r.url());
      if (url.pathname === '/api/review/messages') { gets++; return route.fulfill({ json: { records: messages } }); }
      if (url.pathname.endsWith('/reject') || url.pathname.endsWith('/approve')) {
        if (failReject) return route.fulfill({ status: 500, json: { detail: 'Mock failure' } });
        const id = url.pathname.split('/')[4];
        messages.splice(messages.findIndex(m => m.id === id), 1);
        return route.fulfill({ json: {} });
      }
      if (r.method() === 'PATCH') {
        const message = messages.find(m => url.pathname.endsWith(m.id));
        message.updated_at = 'v2';
        message.effective_output.content.body = r.postDataJSON().body;
        return route.fulfill({ json: message });
      }
      return route.fulfill({ json: { records: [] } });
    });
    page.on('framenavigated', frame => { if (frame === page.mainFrame()) navigations++; });
    page.on('dialog', dialog => dialog.accept());
    await page.goto(process.env.REVIEW_TEST_URL || 'http://127.0.0.1:8000');
    await page.locator('[data-message-id="m2"]').click();
    await page.getByLabel('客户可见正文').fill('Unsaved edit');
    await page.getByLabel('审核备注（可选）').fill('Keep this note');
    await page.locator('[data-message-id="m1"]').click();
    await page.locator('[data-message-id="m2"]').click();
    assert.equal(await page.getByLabel('客户可见正文').inputValue(), 'Unsaved edit');
    assert.equal(await page.getByLabel('审核备注（可选）').inputValue(), 'Keep this note');
    await page.locator('#review-search').fill('Contact 2');
    assert.equal(await page.locator('.record-item.active').getAttribute('data-message-id'), 'm2');
    await page.locator('#review-search').fill('');
    assert.equal(await page.locator('.record-item.active').getAttribute('data-message-id'), 'm2');
    // A manual refresh revalidates data but retains unchanged editors and selection.
    await page.locator('#review-messages-button').click();
    await page.waitForTimeout(100);
    assert.equal(await page.getByLabel('客户可见正文').inputValue(), 'Unsaved edit');
    await page.getByRole('button', { name: '保存修改', exact: true }).click();
    await page.waitForTimeout(100);
    assert.equal(await page.getByLabel('审核备注（可选）').inputValue(), 'Keep this note');
    await page.locator('[data-message-id="m1"]').click();
    await page.locator('[data-message-id="m2"]').click();
    assert.equal(await page.getByLabel('客户可见正文').inputValue(), 'Unsaved edit');
    failReject = true;
    await page.getByRole('button', { name: '拒绝', exact: true }).click();
    await page.waitForTimeout(100);
    assert.equal(await page.locator('.record-item.active').getAttribute('data-message-id'), 'm2');
    assert.equal(await page.locator('.record-item').count(), 3);
    failReject = false;
    const before = gets;
    await page.evaluate(() => { window.firstRow = document.querySelector('[data-message-id="m1"]'); });
    await page.getByRole('button', { name: '拒绝', exact: true }).click();
    await page.waitForTimeout(100);
    assert.equal(gets, before, '成功操作不应重读整个列表');
    assert.equal(await page.locator('.record-item.active').getAttribute('data-message-id'), 'm3');
    assert.ok(await page.evaluate(() => window.firstRow === document.querySelector('[data-message-id="m1"]')), '未操作行应保留 DOM');
    // Changed server versions invalidate cached editors on explicit refresh.
    messages[0].updated_at = 'external-change';
    messages[0].effective_output.content.body = 'Updated elsewhere';
    await page.locator('#review-messages-button').click();
    await page.waitForTimeout(100);
    await page.locator('[data-message-id="m1"]').click();
    assert.equal(await page.getByLabel('客户可见正文').inputValue(), 'Updated elsewhere');
    const beforeApproval = gets;
    await page.getByRole('button', { name: '批准并进入 Outbox', exact: true }).click();
    await page.waitForTimeout(100);
    assert.equal(gets, beforeApproval);
    assert.equal(await page.locator('.record-item.active').getAttribute('data-message-id'), 'm3');
    await page.getByRole('button', { name: '拒绝', exact: true }).click();
    await page.waitForTimeout(100);
    assert.equal(await page.locator('.record-item').count(), 0);
    assert.ok(await page.locator('#detail').innerText().then(text => text.includes('没有等待审阅')));
    assert.equal(navigations, 1, '操作不应触发整页导航');
    console.log('PASS: local list updates, next selection, draft cache, filter retention, failed operation, save, server invalidation, no navigation');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
