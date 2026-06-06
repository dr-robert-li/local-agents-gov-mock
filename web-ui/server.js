// Web UI backend: Fastify static server + SSE relay from Redis + portfolio proxy.
//
// The agent service publishes JSON events to the Redis `agent-stream` channel and
// keeps the last 500 in `agent-stream-replay`. We fan those out to every connected
// browser over Server-Sent Events, and proxy portfolio/session reads to the agent.
import Fastify from 'fastify';
import fastifyStatic from '@fastify/static';
import Redis from 'ioredis';
import { fileURLToPath } from 'url';
import { dirname, join } from 'path';

const __dirname = dirname(fileURLToPath(import.meta.url));
const PORT = parseInt(process.env.PORT || '3000', 10);
const REDIS_URL = process.env.REDIS_URL || 'redis://redis:6379';
const AGENT_BASE = process.env.AGENT_BASE || 'http://agent:8080';
const CHANNEL = 'agent-stream';

const app = Fastify({ logger: true });
await app.register(fastifyStatic, { root: join(__dirname, 'public'), prefix: '/' });

// --- SSE client registry ---
const clients = new Set();

function broadcast(line) {
  for (const res of clients) {
    try { res.write(`data: ${line}\n\n`); } catch { /* dropped client */ }
  }
}

// Dedicated subscriber connection (ioredis requires a separate conn for sub mode).
const sub = new Redis(REDIS_URL, { lazyConnect: true });
const cmd = new Redis(REDIS_URL, { lazyConnect: true });
try {
  await sub.connect();
  await cmd.connect();
  await sub.subscribe(CHANNEL);
  sub.on('message', (_chan, message) => broadcast(message));
  app.log.info('subscribed to redis agent-stream');
} catch (err) {
  app.log.error(`redis connect failed: ${err.message}`);
}

// --- SSE endpoint: replay buffered events, then stream live ---
app.get('/api/stream', async (req, reply) => {
  reply.raw.writeHead(200, {
    'Content-Type': 'text/event-stream',
    'Cache-Control': 'no-cache',
    'Connection': 'keep-alive',
    'X-Accel-Buffering': 'no',
  });
  reply.raw.write('retry: 3000\n\n');

  // Replay the last run so a fresh tab is not blank.
  try {
    const history = await cmd.lrange('agent-stream-replay', 0, -1);
    for (const line of history) reply.raw.write(`data: ${line}\n\n`);
  } catch (err) {
    app.log.error(`replay failed: ${err.message}`);
  }

  clients.add(reply.raw);
  const keepAlive = setInterval(() => {
    try { reply.raw.write(': keep-alive\n\n'); } catch { /* noop */ }
  }, 15000);

  req.raw.on('close', () => {
    clearInterval(keepAlive);
    clients.delete(reply.raw);
  });
});

// --- Portfolio proxy ---
app.get('/api/portfolio', async (_req, reply) => {
  try {
    const r = await fetch(`${AGENT_BASE}/api/portfolio`);
    reply.code(r.status).send(await r.json());
  } catch (err) {
    reply.code(502).send({ error: `agent unreachable: ${err.message}` });
  }
});

// --- Trigger a manual run from the UI ---
app.post('/api/run', async (_req, reply) => {
  try {
    const r = await fetch(`${AGENT_BASE}/run`, { method: 'POST' });
    reply.code(r.status).send(await r.json());
  } catch (err) {
    reply.code(502).send({ error: `agent unreachable: ${err.message}` });
  }
});

app.get('/health', async () => ({ status: 'ok', clients: clients.size }));

app.listen({ port: PORT, host: '0.0.0.0' })
  .then(() => app.log.info(`web-ui on ${PORT}`))
  .catch((err) => { app.log.error(err); process.exit(1); });
