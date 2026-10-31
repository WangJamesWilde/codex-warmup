async page => {
  const frame=page.frameLocator('#app');
  await frame.locator('#enabled').getByText('运行中',{exact:true}).waitFor({timeout:10000});
  await frame.locator('#warning-toggle').getByText('开启低额度提醒',{exact:true}).waitFor();
  await frame.locator('#warning-toggle').click();
  await frame.locator('#warning-toggle').getByText('关闭低额度提醒',{exact:true}).waitFor();
  await frame.locator('#warning-threshold').selectOption('3');
  if((await page.evaluate(()=>window.state.low_quota_warning.threshold))!==3)throw Error('Warning threshold was not saved');
  await frame.locator('#warning-toggle').click();
  await frame.locator('.quick-row').waitFor();
  await frame.locator('.quick-row button').click();
  await frame.locator('.quick-row button[aria-pressed=true]').waitFor();
  const first=await page.evaluate(()=>window.state.selected_chats[0]);
  if(first.message!=='继续'||first.repeat!==false)throw Error('Quick-add must default to continue/single');
  await frame.locator('.quick-row button').click();
  await frame.locator('.quick-row button[aria-pressed=false]').waitFor();
  if((await page.evaluate(()=>window.state.selected_chats.length))!==1)throw Error('Cancellation deleted the rule');
  await frame.locator('#toggle').click();
  await frame.locator('#enabled').getByText('已暂停').waitFor();
  await frame.locator('#toggle').click();
  if(await page.evaluate(()=>calls.some(call=>call.name==='warmup_enable')))throw Error('Enabled before confirmation');
  await frame.locator('#confirm-enable').click();
  await frame.locator('#enabled').getByText('运行中',{exact:true}).waitFor();
  await page.evaluate(()=>{
    window.state.diagnostics={state:'process_exited',process_alive:false,heartbeat_age_seconds:12,
      last_error:{at:Math.floor(Date.now()/1000),stage:'check',type:'MetadataError',method:'account/read',rpc_code:-32602},
      log_path:'D:/fixture/diagnostics.jsonl'};
  });
  await frame.locator('#refresh').click();
  await frame.locator('#health').getByText('后台进程已退出',{exact:false}).waitFor();
  await frame.locator('#enabled').getByText('后台已退出',{exact:true}).waitFor();
  if(await frame.locator('#enabled').getAttribute('class').then(text=>text.includes('on')))throw Error('Dead worker has green running badge');
  await frame.locator('details summary').click();
  if(!await frame.locator('#diagnostic-detail').innerText().then(text=>text.includes('account/read')))throw Error('Diagnostic stage missing');
  await page.evaluate(()=>{delete window.state.diagnostics;});
  await frame.locator('#refresh').click();
  await frame.locator('#enabled').getByText('状态待确认',{exact:true}).waitFor();
  await page.evaluate(()=>{
    window.state.worker.heartbeat=Math.floor(Date.now()/1000)-181;
    window.state.diagnostics={state:'healthy',log_path:'D:/fixture/diagnostics.jsonl'};
  });
  await frame.locator('#refresh').click();
  await frame.locator('#enabled').getByText('后台无响应',{exact:true}).waitFor();
  await frame.locator('#health').getByText('后台心跳已过期',{exact:false}).waitFor();
  await page.evaluate(()=>{window.state.worker.heartbeat=Math.floor(Date.now()/1000);});
  for(const [state,label] of [['starting','正在启动'],['stale','后台无响应'],['missing','后台未启动'],['stopped','后台已停止'],['healthy','运行中']]){
    await page.evaluate(state=>{window.state.diagnostics={state,log_path:'D:/fixture/diagnostics.jsonl'};},state);
    await frame.locator('#refresh').click();
    await frame.locator('#enabled').getByText(label,{exact:true}).waitFor();
  }
  await frame.locator('#add-rule').click();
  await frame.locator('#chat-select option').nth(1).waitFor({state:'attached'});
  await frame.locator('#chat-select').selectOption('019e3130-0000-7000-8000-000000000001');
  await frame.locator('#rule-message').fill('从上一个检查点继续');
  await frame.locator('#save-rule').click();
  await frame.locator('#rules .rule').waitFor();
  await frame.locator('#rules').getByText('预输入 · 额度恢复后发送，正常收尾也可',{exact:true}).waitFor();
  if(await frame.locator('#editor input[type=checkbox]').count())throw Error('Removed checkboxes still present');
  if(!await frame.locator('#rules').innerText().then(text=>text.includes('单次')))throw Error('Default must be single use');
  await frame.locator('#rules [role=switch]').click();
  await frame.locator('#rules [role=switch][aria-checked=false]').waitFor();
  await frame.locator('#rules [role=switch]').click();
  await frame.locator('#rules [role=switch][aria-checked=true]').waitFor();
  await frame.locator('#rules').getByRole('button',{name:'编辑',exact:true}).click();
  await frame.locator('#rule-message').fill('先检查上次的测试结果，再继续');
  await frame.locator('#rule-trigger').selectOption('quota_stop');
  await frame.locator('#save-rule').click();
  await frame.locator('#rules').getByText('额度中断恢复 · 必须确认因额度不足停止',{exact:true}).waitFor();
  await frame.locator('#rules select').selectOption('repeat');
  await frame.locator('#rules').getByText('已启用 · 重复',{exact:true}).waitFor();
  await frame.locator('.quick-row button').click();
  await frame.locator('.quick-row button[aria-pressed=false]').waitFor();
  await frame.locator('.quick-row button').click();
  await frame.locator('.quick-row button[aria-pressed=true]').waitFor();
  const preserved=await page.evaluate(()=>window.state.selected_chats[0]);
  if(preserved.message!=='先检查上次的测试结果，再继续'||preserved.repeat!==true)throw Error('Quick rearm lost custom settings');
  await frame.locator('#rules select').selectOption('count');
  await frame.locator('#rules input[type=number]').fill('3');
  await frame.locator('#rules input[type=number]').press('Enter');
  await frame.locator('#rules').getByText('已启用 · 3/3 次',{exact:true}).waitFor();
  for(const size of [{width:1440,height:900},{width:768,height:1024},{width:375,height:812}]){
    await page.setViewportSize(size);
    const overflow=await frame.locator('html').evaluate(el=>el.scrollWidth>window.innerWidth+2);
    if(overflow)throw Error('Horizontal overflow at '+size.width);
  }
  const calls=await page.evaluate(()=>window.calls);
  if(calls.some(call=>['verify-send','warmup_send','queue'].includes(call.name)))throw Error('UI test sent a real request');
  await page.setViewportSize({width:1440,height:900});
  await page.screenshot({path:'../.state/native-ui-qa.png',fullPage:true});
  await frame.locator('#rules').getByRole('button',{name:'移除',exact:true}).click();
  if((await frame.locator('#rules .rule').count())!==1)throw Error('Removed without confirmation');
  await frame.locator('#rules').getByRole('button',{name:'确认移除规则',exact:true}).click();
  await frame.locator('#rules').getByText('目前没有任何自动发送规则',{exact:true}).waitFor();
  return {passed:true,viewports:3,calls:calls.map(call=>call.name),realModelRequests:0};
}
