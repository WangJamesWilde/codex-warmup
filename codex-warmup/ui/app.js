import {App, applyHostStyleVariables} from '@modelcontextprotocol/ext-apps';
import {OpenAIExtensions} from '@openai/mcp-extensions/app';
import '@openai/mcp-extensions/app/styles.css';

const app = new App({name:'codex-warmup',version:'0.6.0'}, {availableDisplayModes:['fullscreen']});
new OpenAIExtensions(app);
const $ = id => document.getElementById(id);
let state = null;
let busy = false;
let editing = null;
let chats = [];
let nextCursor = null;
let quickChats = [];
let quickCursor = null;
let quickLoaded = false;
let quickRequest = 0;
const modeDrafts = new Map();
const reasons = {
  checking:'正在检查额度', window_already_active:'窗口已经活跃，无需重复预热',
  waiting_for_included_quota:'额度不足，等待恢复', quota_unknown_or_stale:'额度信息不可用，等待刷新',
  waiting_for_observed_reset:'等待服务器确认额度恢复', stopped:'后台已停止', paused:'已暂停',
  attempted_once:'本周期已尝试一次预热', already_attempted_or_paused:'本周期已处理，未重复发送',
  warmup_model_unavailable:'Luna 暂不可用，保留待处理窗口', paused_account_changed:'登录账号变化，已自动暂停',
  enabled:'预热已启用', paused_by_user:'预热已暂停', sending_once:'发送一次预热请求',
  request_finished:'预热请求结束', check_failed:'检查未完成，稍后再试',
  observed_extra_recovery:'检测到额外额度恢复', observed_blocking_quota_recovered:'额度阻塞已解除',
  resume_rule_saved:'已保存续聊规则', resume_queued:'续聊消息已进入队列',
  resume_unconfirmed_no_retry:'续聊投递未确认，不自动重试', resume_cancelled_state_changed:'对话状态改变，已取消投递',
  resume_rule_armed:'已加入下一次自动续聊', resume_rule_disabled:'已取消自动续聊（保留配置）',
  resume_rule_mode_changed:'已修改续聊执行方式',
  recovery_bridge_configured:'已配置 Aion 协作续接',
  resume_bridge_unavailable:'协作桥不可用，未发送、未消耗规则次数',
  resume_bridge_ambiguous:'协作归属不明确，未发送', resume_bridge_blocked:'协作恢复被阻止，请查看任务状态',
  resume_bridge_accepted:'协作桥已受理，等待原生队列回执', resume_bridge_queued:'协作恢复已进入原生队列',
  resume_bridge_processing:'协作恢复回合已开始', resume_bridge_completed:'协作恢复回合已完成',
  resume_bridge_failed:'协作恢复失败，请查看原对话', resume_bridge_unknown:'协作恢复结果未知，不自动重发',
  worker_launched:'后台进程已启动', worker_started:'后台开始检查', worker_exited:'后台已退出（详情见诊断）',
  worker_failed:'后台异常退出', worker_launch_failed:'后台启动失败', worker_cleanup_failed:'后台退出清理失败',
  worker_auto_recovered:'插件重新加载，已恢复原先启用的后台', worker_recovery_failed:'后台自动恢复失败，详见诊断',
  warmup_failed:'预热请求未确认，详见诊断；不会自动重发',
  resume_recovery_observed:'已记录额度恢复（包括重新启用期间）', paused_for_upgrade:'升级期间短暂停止后台', upgrade_finished:'插件升级完成',
};
const skipNotes={waiting_recovery_evidence:'尚未取得额度恢复证据，未发送',waiting_next_recovery:'等待下一次额度恢复',
  desktop_not_running:'Codex桌面程序未运行，保留待发送',quota_unavailable:'额度不可用，未发送',
  chat_busy_or_unknown:'对话正在运行或状态不明确，未发送',no_quota_stop_evidence:'没有额度中断记录，未发送',
  not_proven_quota_stop:'最新回合并非已确认的额度中断（或已经手动续上）',chat_metadata_unknown:'缺少对话元数据，未发送',
  goal_paused_or_not_actionable:'对话目标已暂停或受预算限制，未发送',already_attempted:'此额度中断已处理，不重复投递'};
Object.assign(skipNotes,{preinput_anchor_changed:'单次预输入保存后已有新一轮工作，未发送；如仍需要，请重新编辑保存',
  chat_progressed_after_recovery:'对话在额度恢复后仍有进展，不重复启动',not_safe_preinput_target:'不是正常完成或明确额度中断，未发送'});
function error(message){$('error').textContent=message;$('error').hidden=!message;}
function localDate(seconds){return seconds?new Date(seconds*1000).toLocaleString('zh-CN',{hour12:false}):'未知';}
function hostStyle(context){
  if(context?.styles?.variables) applyHostStyleVariables(context.styles.variables);
  const insets=context?.safeAreaInsets;
  if(insets){document.documentElement.style.setProperty('--safe-top',`${insets.top||0}px`);document.documentElement.style.setProperty('--safe-bottom',`${insets.bottom||0}px`);}
}
function render(value){
  if(!value||typeof value.enabled!=='boolean') return;
  state=value;
  const diagnosis=value.diagnostics;
  const age=value.worker?.heartbeat?Math.max(0,Math.floor(Date.now()/1000-value.worker.heartbeat)):Infinity;
  const reportedHealth=diagnosis?.state||'unknown';
  const healthState=!value.enabled?'paused':reportedHealth==='healthy'&&age>180?'stale':reportedHealth;
  const badges={paused:'已暂停',healthy:'运行中',starting:'正在启动',process_exited:'后台已退出',
    stopped:'后台已停止',missing:'后台未启动',stale:'后台无响应',unknown:'状态待确认'};
  $('enabled').textContent=badges[healthState]||badges.unknown;
  $('enabled').className=`badge ${healthState==='healthy'?'on':healthState==='paused'?'':'warn'}`;
  $('enabled').title=value.enabled?'后台开关已开启；此处显示实际进程和心跳状态':'后台开关已关闭，不会自动恢复';
  $('toggle').textContent=value.enabled?'暂停后台':'启用后台';
  $('toggle').disabled=busy;
  $('autostart').textContent=value.login_autostart?'已开启':'未开启';
  const warning=value.low_quota_warning||{enabled:false,threshold:5};
  $('warning-threshold').value=String(warning.threshold);
  $('warning-toggle').textContent=warning.enabled?'关闭低额度提醒':'开启低额度提醒';
  $('warning-toggle').disabled=busy;
  for(const key of ['primary','secondary']){
    const quota=value.readable_quota?.[key];
    $(key).textContent=quota?String(Math.round(quota.remaining_percent*10)/10):'—';
    $(key+'-bar').value=quota?Math.max(0,Math.min(100,quota.remaining_percent)):0;
    $(key+'-reset').textContent=quota?`下次重置：${new Date(quota.resets_local).toLocaleString('zh-CN',{hour12:false})}`:'等待服务器额度信息';
  }
  const healthNotes={paused:'后台已暂停，不会预热或续聊',starting:'后台正在启动',process_exited:'后台进程已退出；不能仅凭旧心跳判断正常',
    stopped:'后台已停止',missing:'尚无后台心跳',stale:'后台心跳已过期，需检查诊断',unknown:'后台健康状态尚未确认'};
  $('health').textContent=healthNotes[healthState]||(reasons[value.worker?.state]||'等待下一次检查');
  $('health-dot').className=`dot ${healthState==='healthy'?'':'warn'}`;
  const diagLines=[];
  if(diagnosis){
    diagLines.push(`进程：${value.worker?.pid||'未知'} · 心跳距今：${diagnosis.heartbeat_age_seconds??'未知'} 秒`);
    const last=diagnosis.last_error;
    if(last)diagLines.push(`最近异常：${localDate(last.at)} · ${last.stage} · ${last.type}${last.method?' · '+last.method:''}${last.rpc_code!==undefined?' · RPC '+last.rpc_code:''}${last.winerror!==undefined?' · Windows '+last.winerror:''}`);
    const exit=diagnosis.last_exit;
    if(exit)diagLines.push(`最近退出：${localDate(exit.at)} · ${exit.reason==='paused'?'已暂停':exit.reason==='lease_owned_elsewhere'?'另一后台实例持有运行权':exit.reason==='fatal_error'?'进程内部异常':exit.reason}`);
    if(diagnosis.state==='process_exited')diagLines.push('若没有对应退出记录，可能是外部终止或崩溃；现有日志不能据此断定具体原因。');
    diagLines.push(`诊断日志：${diagnosis.log_path}`);
  }else diagLines.push('当前后台尚未提供详细诊断。');
  $('diagnostic-detail').textContent=diagLines.join('\n');
  $('sample-age').textContent=value.quota?.at?`快照：${localDate(value.quota.at)}`:'尚无额度快照';
  $('events').replaceChildren();
  $('events').className='';
  for(const event of value.events||[]){
    const row=document.createElement('div');row.className='log';
    const time=document.createElement('time');time.textContent=localDate(event.at);
    const label=document.createElement('span');label.textContent=reasons[event.event]||event.event;
    row.append(time,label);$('events').append(row);
  }
  if(!value.events?.length){$('events').textContent='暂无记录';$('events').className='empty';}
  renderRules(value.selected_chats||[]);
  renderQuickChats();
}
function currentRule(chatId){return state?.selected_chats?.find(rule=>rule.thread_id===chatId&&rule.account===state.quota?.account);}
function ruleMode(rule){return rule.run_limit===null?'repeat':rule.run_limit>1?'count':'once';}
function modeLabel(rule){return ruleMode(rule)==='repeat'?'重复':ruleMode(rule)==='count'?`${rule.remaining_runs}/${rule.run_limit} 次`:'单次';}
function quickButton(chatId, rule=currentRule(chatId)){
  const button=document.createElement('button');button.className='quick-toggle';button.dataset.ruleToggle='';
  button.setAttribute('aria-pressed',String(!!rule?.enabled));
  button.textContent=rule?.enabled?'✓ 已加入':'加入续聊';
  button.title=rule?.enabled?'取消尚未投递的自动续聊，保留消息设置；不撤回已投递的消息':(rule?'沿用已保存的消息和重复设置，加入下一次自动续聊':'默认“继续／单次”，加入下一次自动续聊，不立即发送');
  button.disabled=busy;
  button.onclick=()=>action(async()=>{
    const result=rule?await call('warmup_rule_set_enabled',{thread_id:chatId,enabled:!rule.enabled,
      expected_revision:rule.revision,...(!rule.enabled?{confirm_send:true}:{})}):
      await call('warmup_rule_save',{thread_id:chatId,message:'继续',run_limit:1,trigger:'after_recovery',enabled:true,expected_revision:0,confirm_send:true});
    if(result.conflict){render(await call('warmup_status'));throw Error('这条规则刚被修改，已刷新状态，请重新确认后操作。');}
  });
  return button;
}
function renderQuickChats(){
  if(!quickLoaded)return;
  const list=$('quick-list');list.replaceChildren();
  $('quick-count').textContent=`${quickChats.length} 个${quickCursor?'（可加载更多）':''}`;
  for(const chat of quickChats){
    const rule=currentRule(chat.id);
    const row=document.createElement('div');row.className='quick-row';row.dataset.threadId=chat.id;
    const info=document.createElement('div');info.className='quick-info';
    const title=document.createElement('div');title.className='quick-title';title.textContent=chat.title;title.title=chat.title;
    const detail=document.createElement('div');detail.className='quick-detail';
    const project=chat.cwd?.replaceAll('\\','/').split('/').filter(Boolean).pop();
    detail.textContent=[project,rule?`${rule.message==='继续'?'继续':'自定义消息'} · ${modeLabel(rule)}`:'默认：继续 · 单次'].filter(Boolean).join(' · ');
    info.append(title,detail);row.append(info,quickButton(chat.id,rule));list.append(row);
  }
  if(!quickChats.length){const empty=document.createElement('div');empty.className='empty';empty.textContent='没有找到对话，请换个名称搜索。';list.append(empty);}
  $('quick-more').hidden=!quickCursor;
}
async function loadQuickChats(more=false){
  const request=++quickRequest;
  $('quick-refresh').disabled=true;$('quick-search').disabled=true;$('quick-more').disabled=true;
  try{
    const result=await call('warmup_chats',{query:$('quick-query').value,...(more&&quickCursor?{cursor:quickCursor}:{})});
    if(request!==quickRequest)return;
    quickChats=more?[...new Map([...quickChats,...result.chats].map(chat=>[chat.id,chat])).values()]:result.chats;
    quickCursor=result.next_cursor;quickLoaded=true;renderQuickChats();
  }catch(e){error(e.message);}
  finally{if(request===quickRequest){$('quick-refresh').disabled=false;$('quick-search').disabled=false;$('quick-more').disabled=false;}}
}
function renderRules(rules){
  $('rule-count').textContent=`${rules.length} 项`;
  if(!busy&&document.activeElement?.closest('#rules .count-input, #rules .mode-select'))return;
  $('rules').replaceChildren();$('rules').className='';
  if(!rules.length){
    $('rules').className='empty';
    const title=document.createElement('strong');title.textContent='目前没有任何自动发送规则';
    $('rules').append(title,document.createTextNode('添加规则后，每条消息独立设置；默认只发送一次。'));
    return;
  }
  for(const rule of rules){
    const row=document.createElement('div');row.className='rule';
    const head=document.createElement('div');head.className='rulehead';
    const name=document.createElement('span');name.className='ruletitle';name.textContent=rule.title;
    const toggle=quickButton(rule.thread_id,rule);toggle.className='rule-switch';toggle.setAttribute('role','switch');
    toggle.removeAttribute('aria-pressed');toggle.setAttribute('aria-checked',String(rule.enabled));
    toggle.setAttribute('aria-label',`启用规则：${rule.title}`);toggle.replaceChildren();
    const toggleLabel=document.createElement('span');toggleLabel.textContent=`${rule.enabled?'已启用':'已停用'} · ${modeLabel(rule)}`;
    const track=document.createElement('span');track.className='switch-track';track.setAttribute('aria-hidden','true');
    toggle.append(toggleLabel,track);head.append(name,toggle);
    const message=document.createElement('div');message.className='rulemsg';message.textContent=rule.message;
    const condition=document.createElement('div');condition.className='foot';
    condition.textContent=rule.trigger==='after_recovery'?'预输入 · 额度恢复后发送，正常收尾也可':'额度中断恢复 · 必须确认因额度不足停止';
    const foot=document.createElement('div');foot.className='rulehead';
    const note=document.createElement('span');note.className='rulefooter';
    const result=rule.last_result;
    const skip=rule.enabled?state?.resume_diagnostics?.[rule.id]:null;
    const bridgeNotes={bridge_unavailable:'协作桥不可用；未发送，未消耗本次次数',bridge_ambiguous:'协作归属不明确；未发送',
      bridge_blocked:'协作恢复被阻止；不回退发送普通消息',bridge_accepted:'协作桥已受理；尚无原生队列回执',
      bridge_queued:'协作消息已排队；不代表任务已完成',bridge_processing:'协作恢复回合已开始',
      bridge_completed:'协作恢复回合已完成；不代表整个任务验收通过',bridge_failed:'协作恢复失败；请查看原对话',
      bridge_unknown:'协作恢复结果未知；只查询回执，不重新投递'};
    note.textContent=result?(bridgeNotes[result.state]||(result.state==='queued'?'已排队；队列接收不等于任务完成':result.state==='cancelled_state_changed'?'状态改变，未发送':'投递结果未确认，不自动重试')):(rule.enabled?'等待下一次额度恢复':'已停用，消息和重复设置已保留');
    if(skipNotes[skip?.reason])note.textContent=skipNotes[skip.reason];
    const buttons=document.createElement('div');buttons.className='rulebuttons';
    const edit=document.createElement('button');edit.textContent='编辑';edit.onclick=()=>openEditor(rule);
    const mode=document.createElement('select');mode.className='mode-select';mode.setAttribute('aria-label','续聊执行方式');
    for(const [value,label] of [['once','单次'],['repeat','每次额度恢复后重复'],['count','指定次数']]){const option=document.createElement('option');option.value=value;option.textContent=label;mode.append(option);}
    const draft=modeDrafts.get(rule.id);mode.value=draft?.mode||ruleMode(rule);
    const count=document.createElement('input');count.className='count-input';count.type='number';count.min='1';count.max='1000';count.step='1';
    count.setAttribute('aria-label','续聊次数');count.title='输入次数后按 Enter 或移开焦点保存';
    count.value=draft?.value||String(rule.run_limit>1?rule.run_limit:3);count.hidden=mode.value!=='count';
    const commit=()=>{
      const limit=mode.value==='repeat'?null:mode.value==='once'?1:Number(count.value);
      if(limit!==null&&(!Number.isInteger(limit)||limit<1||limit>1000)){error('次数需要填写 1 到 1000 的整数。');return;}
      if(limit===rule.run_limit){modeDrafts.delete(rule.id);return;}
      action(async()=>{
        const result=await call('warmup_rule_set_mode',{rule_id:rule.id,expected_revision:rule.revision,run_limit:limit,confirm_send:true});
        if(result.conflict){render(await call('warmup_status'));throw Error('规则已变化，已刷新，请重新选择执行方式。');}
        modeDrafts.delete(rule.id);
      });
    };
    mode.onchange=()=>{count.hidden=mode.value!=='count';if(mode.value==='count'){modeDrafts.set(rule.id,{mode:'count',value:count.value});count.focus();count.select();}else{modeDrafts.delete(rule.id);commit();}};
    count.oninput=()=>modeDrafts.set(rule.id,{mode:'count',value:count.value});
    count.onchange=commit;count.onkeydown=event=>{if(event.key==='Enter'){event.preventDefault();commit();}};
    const remove=document.createElement('button');remove.textContent='移除';
    remove.onclick=()=>{
      buttons.replaceChildren();
      const confirm=document.createElement('button');confirm.className='danger';confirm.textContent='确认移除规则';
      const cancel=document.createElement('button');cancel.textContent='取消';cancel.onclick=()=>renderRules(state.selected_chats||[]);
      confirm.onclick=()=>action(()=>call('warmup_rule_remove',{rule_id:rule.id}));
      buttons.append(confirm,cancel);
    };
    buttons.append(edit,mode,count,remove);foot.append(note,buttons);row.append(head,condition,message,foot);$('rules').append(row);
  }
}
function chatOptions(){
  const current=$('chat-select').value;$('chat-select').replaceChildren();
  const placeholder=document.createElement('option');placeholder.value='';placeholder.textContent=chats.length?'请选择一个对话':'没有找到符合条件的对话';$('chat-select').append(placeholder);
  for(const chat of chats){const option=document.createElement('option');option.value=chat.id;option.textContent=`${chat.title}${chat.model?' · '+chat.model:''}`;$('chat-select').append(option);}
  if(chats.some(chat=>chat.id===current))$('chat-select').value=current;
  $('more-chats').hidden=!nextCursor;
}
async function loadChats(more=false){
  $('search-chats').disabled=true;
  try{
    const result=await call('warmup_chats',{query:$('chat-query').value,...(more&&nextCursor?{cursor:nextCursor}:{})});
    chats=more?[...chats,...result.chats]:result.chats;nextCursor=result.next_cursor;chatOptions();
  }catch(e){error(e.message);}finally{$('search-chats').disabled=false;}
}
function openEditor(rule=null){
  editing=rule;$('editor').hidden=false;$('editor-title').textContent=rule?'编辑续聊规则':'添加续聊规则';
  $('rule-message').value=rule?.message||'继续';
  $('rule-trigger').value=rule?(rule.trigger||'quota_stop'):'after_recovery';
  $('chat-query').value='';$('chat-select').disabled=!!rule;$('search-chats').hidden=!!rule;
  $('chat-query').disabled=!!rule;nextCursor=null;
  if(rule){chats=[{id:rule.thread_id,title:rule.title}];chatOptions();$('chat-select').value=rule.thread_id;}
  else{chats=[];chatOptions();loadChats();}
  $('editor').scrollIntoView({behavior:'smooth',block:'nearest'});
}
function unpack(result){
  if(result.isError) throw Error('操作没有完成，请检查本地后台状态。');
  if(result.structuredContent) return result.structuredContent;
  const item=result.content?.find(item=>item.type==='text');
  return item?JSON.parse(item.text):null;
}
async function call(name,args={}){return unpack(await app.callServerTool({name,arguments:args}));}
async function action(run){
  if(busy)return;
  busy=true;error('');$('toggle').disabled=true;$('refresh').disabled=true;
  $('warning-toggle').disabled=true;$('warning-threshold').disabled=true;
  document.querySelectorAll('[data-rule-toggle]').forEach(button=>button.disabled=true);
  try{
    const extra=await run();const value=await call('warmup_status');
    if(extra?.snapshot){
      value.quota=extra.snapshot;value.readable_quota={};
      for(const key of ['primary','secondary']){const w=extra.snapshot[key];if(typeof w?.usedPercent==='number'&&w.resetsAt)value.readable_quota[key]={remaining_percent:100-w.usedPercent,resets_local:new Date(w.resetsAt*1000).toISOString()};}
    }
    render(value);
  }
  catch(e){error(e.message||'连接失败，请稍后刷新。');}
  finally{busy=false;$('toggle').disabled=!state;$('refresh').disabled=false;$('warning-toggle').disabled=!state;$('warning-threshold').disabled=false;document.querySelectorAll('[data-rule-toggle]').forEach(button=>button.disabled=false);}
}
$('refresh').onclick=()=>action(()=>call('warmup_refresh'));
$('warning-toggle').onclick=()=>action(()=>call('warmup_warning_configure',{
  enabled:!state?.low_quota_warning?.enabled,threshold:Number($('warning-threshold').value)}));
$('warning-threshold').onchange=()=>action(()=>call('warmup_warning_configure',{
  enabled:!!state?.low_quota_warning?.enabled,threshold:Number($('warning-threshold').value)}));
$('toggle').onclick=()=>state?.enabled?action(()=>call('warmup_pause')):($('confirm').hidden=false);
$('cancel-enable').onclick=()=>{$('confirm').hidden=true;};
$('confirm-enable').onclick=()=>action(async()=>{$('confirm').hidden=true;await call('warmup_enable',{confirm_quota_spend:true});});
$('add-rule').onclick=()=>openEditor();
$('cancel-rule').onclick=()=>{$('editor').hidden=true;};
$('search-chats').onclick=()=>loadChats();
$('chat-query').onkeydown=event=>{if(event.key==='Enter'){event.preventDefault();loadChats();}};
$('more-chats').onclick=()=>loadChats(true);
$('quick-refresh').onclick=()=>loadQuickChats();
$('quick-search').onclick=()=>loadQuickChats();
$('quick-query').onkeydown=event=>{if(event.key==='Enter'){event.preventDefault();loadQuickChats();}};
$('quick-more').onclick=()=>loadQuickChats(true);
$('save-rule').onclick=()=>{
  const thread=$('chat-select').value,message=$('rule-message').value.trim();
  if(!thread||!message){error('请选择对话并填写消息。');return;}
  action(async()=>{
    const original=editing||currentRule(thread);
    const result=await call('warmup_rule_save',{thread_id:thread,message,run_limit:original?original.run_limit:1,enabled:true,
      trigger:$('rule-trigger').value,
      expected_revision:original?.revision||0,confirm_send:true});
    if(result.conflict){render(await call('warmup_status'));throw Error('规则已变化，请重新打开编辑后保存。');}
    $('editor').hidden=true;
  });
};
app.ontoolresult=result=>{try{render(unpack(result));}catch(e){error(e.message);}};
app.onhostcontextchanged=hostStyle;
app.connect().then(async()=>{hostStyle(app.getHostContext());if(!state)await action(async()=>{});await loadQuickChats();}).catch(()=>error('当前宿主没有完成 MCP App 连接。请从 Codex 插件侧边栏入口打开，而不是普通浏览器。'));
const timer=setInterval(()=>{if(!busy)call('warmup_status').then(render).catch(()=>error('暂时无法读取后台状态，稍后会重新检查。'));},15000);
window.addEventListener('pagehide',()=>clearInterval(timer));
document.modelContext?.registerTool({name:'read_warmup_panel',description:'Read the visible warm-up status without sending a model request.',inputSchema:{type:'object',properties:{}},annotations:{readOnlyHint:true},async execute(){return JSON.stringify({enabled:state?.enabled,quota:state?.readable_quota,worker:state?.worker?.state,selectedChats:state?.selected_chats?.map(rule=>({title:rule.title,message:rule.message,repeat:rule.repeat,enabled:rule.enabled}))||[]});}});
