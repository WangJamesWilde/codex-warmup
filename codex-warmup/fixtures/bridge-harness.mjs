// Opt-in integration fixture: real bridge protocol, isolated SQLite and fake AI.
import { DatabaseSync } from 'node:sqlite';
import { copyFileSync, mkdirSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { createServer } from 'node:http';
import { createInterface } from 'node:readline';

const [source, root] = process.argv.slice(2);
const load = relative => import(pathToFileURL(join(source, relative)).href);
const { Store } = await load('src/store.mjs');
const { RecoveryGateway } = await load('src/recovery.mjs');
const { Engine } = await load('src/engine.mjs');
const { MemberGateway } = await load('src/member-gateway.mjs');
const threadId = '11111111-1111-4111-8111-111111111111';
const failedTurnId = '22222222-2222-4222-8222-222222222222';
const resumedTurnId = '33333333-3333-4333-8333-333333333333';
const privateThreadId = '44444444-4444-4444-8444-444444444444';
const privateTurnId = '55555555-5555-4555-8555-555555555555';
const actualNow = Date.now.bind(Date);
let shift = 0;
Date.now = () => actualNow() + shift;
mkdirSync(join(root, 'data'), { recursive: true });
const busPath = join(root, 'fixture-bus.sqlite');
const bus = new DatabaseSync(busPath);
bus.exec('CREATE TABLE channels(id INTEGER PRIMARY KEY,name TEXT UNIQUE,topic TEXT,created_at TEXT,updated_at TEXT);CREATE TABLE messages(id INTEGER PRIMARY KEY,channel_id INTEGER,ts TEXT,author TEXT,type TEXT,content TEXT,reply_to INTEGER,meta TEXT)');
bus.close();
const store = new Store(join(root, 'fixture-bridge.sqlite'), busPath);
const group = store.create('Isolated recovery QA', [
  { id: 'a', provider: 'codex', native_session_id: threadId },
  { id: 'b', provider: 'dsh', native_session_id: 'fixture-other' },
]);
store.chat(group.id, ['a'], 'FIXTURE ORIGINAL IMPLEMENTATION', 'fixture-original');
const delivery = store.deliveries()[0];
const native = { turnId: failedTurnId, status: 'failed', quotaExceeded: true,
  bridgeTagged: true, deliveryId: delivery.id, mixedInput: false };
let privateQueued = false;
const gateway = new RecoveryGateway(store, () => ({ latestTurn: () => native,
  preinputTurn: id => {
    if(id!==privateThreadId)throw Error('FIXTURE_NOT_A_PRIVATE_THREAD');
    return {turnId:privateTurnId,status:'completed',completionConfirmed:true,quotaExceeded:false,
      bridgeTagged:false,deliveryId:null,mixedInput:false,queuedInput:privateQueued,inputAfterCompletion:false};
  },
}));
const members = new MemberGateway(store, root);
const calls = [];
const adapter = {
  busy: () => false, cursor: () => ({ seq: calls.length }),
  send: async (_member, text) => { calls.push(text); return { queueId: `fixture-submission-${calls.length}` }; },
  reply: () => ({ turnId: failedTurnId, status: 'failed', error: 'CODEX_USAGE_LIMIT_EXCEEDED' }),
};
const engine = new Engine(store, { codex: adapter }, members, gateway);
await engine.tick();
await engine.tick();
const before = store.discussions()[0];
const unlimited = before.deadline === null && before.wake_limit === null;
if (!unlimited && !(before.quota_remaining > 0)) throw Error('FIXTURE_QUOTA_FREEZE_MISSING');
shift = (unlimited ? 48 : 3) * 3600000;
if (store.discussions()[0].status !== 'active') throw Error('FIXTURE_CROSS_HOUR_EXPIRED');
const token = 'synthetic-fixture-token-not-a-user-credential';
const server = createServer(async (req, res) => {
  try {
    if (req.url !== '/recovery' || req.headers.authorization !== `Bearer ${token}`) {
      res.writeHead(403); res.end('{"error":"FIXTURE_FORBIDDEN"}'); return;
    }
    let body = '';
    for await (const part of req) { body += part; if (body.length > 40000) throw Error('FIXTURE_TOO_LARGE'); }
    res.setHeader('content-type', 'application/json');
    res.end(JSON.stringify(gateway.handle(JSON.parse(body))));
  } catch {
    res.writeHead(400); res.end('{"error":"FIXTURE_INVALID_REQUEST"}');
  }
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const cli = join(root, 'recovery-cli.mjs');
copyFileSync(join(source, 'recovery-cli.mjs'), cli);
writeFileSync(join(root, 'data/runtime.json'), JSON.stringify({ url: `http://127.0.0.1:${server.address().port}`, token }));
const output = value => process.stdout.write(JSON.stringify(value) + '\n');
output({ ready: true, cli, threadId, failedTurnId, privateThreadId, privateTurnId, remaining: before.quota_remaining, unlimited, elapsedHours: shift / 3600000 });
try {
  for await (const line of createInterface({ input: process.stdin })) {
    const command = JSON.parse(line);
    if (command.action === 'stop') break;
    if(command.action==='private_queue' || command.action==='private_idle') {
      privateQueued=command.action==='private_queue';
      output({calls:calls.length,recoveryAttempts:store.db.prepare('SELECT COUNT(*) AS n FROM recovery_attempts').get().n});
      continue;
    }
    if (command.action === 'complete') {
      adapter.reply = () => ({ turnId: resumedTurnId, status: 'completed', text: 'FIXTURE COMPLETED' });
    }
    await engine.tick();
    const discussion = store.discussions()[0];
    output({ calls: calls.length, hasCustomMessage: calls.at(-1)?.includes(command.message || 'never-match'),
      replayedOriginal: calls.slice(1).some(text => text.includes('FIXTURE ORIGINAL IMPLEMENTATION')),
      completions: store.messages(group.id).filter(message => message.content === 'FIXTURE COMPLETED').length,
      quotaRemaining: discussion.quota_remaining, wakeUsed: discussion.wake_used,
      originalRemaining: before.quota_remaining,
      resumedRemaining: discussion.deadline === null ? null : discussion.deadline - Date.now() });
  }
} finally {
  await new Promise(resolve => server.close(resolve));
  store.close();
}
