'use strict';

/**
 * Yandex Cloud Function bridge for VK Callback API.
 *
 * Production default: enqueue VK Callback events to Yandex Message Queue and
 * return literal "ok" immediately. A VM-side worker polls the queue, calls the
 * local Hermes API Server (127.0.0.1), and sends replies via VK API. This keeps
 * Hermes private: no public API Server exposure is required.
 *
 * Entry point: index.handler
 * Runtime: Node.js 18+
 *
 * Required VK env:
 *   VK_GROUP_ID
 *   VK_CONFIRMATION_TOKEN
 *   VK_SECRET                         optional but strongly recommended
 *
 * Queue mode env (recommended):
 *   BRIDGE_MODE=queue
 *   QUEUE_URL                         Yandex Message Queue URL
 *   AWS_ACCESS_KEY_ID                 static key for SA with ymq.writer
 *   AWS_SECRET_ACCESS_KEY
 *   AWS_REGION=ru-central1
 *
 * Legacy/smoke env (optional, not recommended for production):
 *   BRIDGE_MODE=sync|fire_and_forget
 *   VK_GROUP_TOKEN
 *   HERMES_API_BASE
 *   HERMES_API_KEY
 *   HERMES_MODEL=hermes-agent
 *   HERMES_TIMEOUT_MS=120000
 *   VK_API_VERSION=5.199
 *   BRIDGE_INTERNAL_URL
 *   BRIDGE_INTERNAL_SECRET
 *   BRIDGE_ENQUEUE_TIMEOUT_MS=1500
 */

const crypto = require('crypto');
const { SQSClient, SendMessageCommand } = require('@aws-sdk/client-sqs');

const VK_API_VERSION = env('VK_API_VERSION', '5.199');
const HERMES_MODEL = env('HERMES_MODEL', 'hermes-agent');
const HERMES_TIMEOUT_MS = intEnv('HERMES_TIMEOUT_MS', 120000);
const BRIDGE_ENQUEUE_TIMEOUT_MS = intEnv('BRIDGE_ENQUEUE_TIMEOUT_MS', 1500);
const VK_MAX_MESSAGE_CHARS = 9000;

let sqsClient = null;

function env(name, fallback = '') {
  const value = process.env[name];
  return value == null || value === '' ? fallback : value;
}

function intEnv(name, fallback) {
  const raw = env(name, '');
  const parsed = Number.parseInt(raw, 10);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function response(statusCode, body, contentType = 'text/plain; charset=utf-8') {
  return {
    statusCode,
    headers: {
      'content-type': contentType,
      'cache-control': 'no-store',
    },
    body: typeof body === 'string' ? body : JSON.stringify(body),
  };
}

function parseBody(event) {
  if (!event) return {};
  let body = event.body;
  if (event.isBase64Encoded && typeof body === 'string') {
    body = Buffer.from(body, 'base64').toString('utf8');
  }
  if (body == null || body === '') return {};
  if (typeof body === 'object') return body;
  try {
    return JSON.parse(body);
  } catch (_err) {
    return null;
  }
}

function header(event, name) {
  const headers = event && event.headers ? event.headers : {};
  const wanted = name.toLowerCase();
  for (const [key, value] of Object.entries(headers)) {
    if (String(key).toLowerCase() === wanted) return String(value);
  }
  return '';
}

function timingSafeEqualString(a, b) {
  const left = Buffer.from(String(a || ''), 'utf8');
  const right = Buffer.from(String(b || ''), 'utf8');
  if (left.length !== right.length) return false;
  return crypto.timingSafeEqual(left, right);
}

function validateVkPayload(payload) {
  const groupId = env('VK_GROUP_ID');
  const secret = env('VK_SECRET');

  if (!payload || typeof payload !== 'object') {
    return { ok: false, statusCode: 400, body: 'bad json' };
  }
  if (groupId && String(payload.group_id || '') !== String(groupId)) {
    return { ok: false, statusCode: 403, body: 'invalid group' };
  }
  if (secret && !timingSafeEqualString(payload.secret || '', secret)) {
    return { ok: false, statusCode: 403, body: 'invalid secret' };
  }
  return { ok: true };
}

function normalizeVkMessage(payload) {
  const object = payload.object || {};
  const message = object.message || object;
  const peerId = String(message.peer_id || message.user_id || message.from_id || '');
  const fromId = String(message.from_id || message.user_id || peerId || '');
  const text = String(message.text || message.body || '').trim();
  const messageId = String(message.id || message.conversation_message_id || payload.event_id || '');
  const attachments = Array.isArray(message.attachments) ? message.attachments : [];
  return { message, peerId, fromId, text, messageId, attachments };
}

function isHelpCommand(text) {
  const normalized = text.trim().toLowerCase();
  return normalized === 'начать' || normalized === '/start' || normalized === 'помощь' || normalized === '/help';
}

function helpText() {
  return [
    'Привет! Я VK-канал связи с Hermes Agent.',
    '',
    'Напиши обычное сообщение — я передам его агенту и верну ответ сюда.',
    'Команды: /help, помощь, /start, начать.',
  ].join('\n');
}

function buildHermesInput(vk) {
  const attachmentSummary = vk.attachments.length
    ? `\n\n[VK attachments: ${vk.attachments.map((a) => a && a.type).filter(Boolean).join(', ')}]`
    : '';
  return `${vk.text || '[empty VK message]'}${attachmentSummary}`;
}

function hermesInstructions(vk) {
  return [
    'Ты отвечаешь пользователю через VK community messages.',
    'Пиши на русском, кратко и по делу, если пользователь не просит подробно.',
    'Не используй Telegram MarkdownV2; VK поддерживает обычный текст и ссылки.',
    `VK peer_id: ${vk.peerId}; VK from_id: ${vk.fromId}.`,
  ].join('\n');
}

function extractHermesText(data) {
  if (!data || typeof data !== 'object') return '';

  if (Array.isArray(data.output)) {
    const parts = [];
    for (const item of data.output) {
      if (!item || item.type !== 'message' || !Array.isArray(item.content)) continue;
      for (const content of item.content) {
        if (content && (content.type === 'output_text' || content.type === 'text') && content.text) {
          parts.push(String(content.text));
        }
      }
    }
    if (parts.length) return parts.join('\n').trim();
  }

  const choice = data.choices && data.choices[0];
  if (choice && choice.message && choice.message.content) {
    return String(choice.message.content).trim();
  }

  return '';
}

async function fetchWithTimeout(url, options, timeoutMs) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(url, { ...options, signal: controller.signal });
  } finally {
    clearTimeout(timer);
  }
}

async function callHermes(vk) {
  const base = env('HERMES_API_BASE').replace(/\/+$/, '');
  const key = env('HERMES_API_KEY');
  if (!base || !key) {
    throw new Error('HERMES_API_BASE and HERMES_API_KEY are required');
  }

  const payload = {
    model: HERMES_MODEL,
    input: buildHermesInput(vk),
    instructions: hermesInstructions(vk),
    conversation: `vk:${vk.peerId}`,
    store: true,
  };

  const res = await fetchWithTimeout(`${base}/v1/responses`, {
    method: 'POST',
    headers: {
      authorization: `Bearer ${key}`,
      'content-type': 'application/json',
      'x-hermes-session-key': `vk:${vk.peerId}`,
    },
    body: JSON.stringify(payload),
  }, HERMES_TIMEOUT_MS);

  const text = await res.text();
  let data;
  try { data = JSON.parse(text); } catch (_err) { data = { raw: text }; }

  if (!res.ok) {
    const safe = typeof text === 'string' ? text.slice(0, 500) : JSON.stringify(data).slice(0, 500);
    throw new Error(`Hermes API HTTP ${res.status}: ${safe}`);
  }

  const answer = extractHermesText(data);
  if (!answer) throw new Error('Hermes API returned no assistant text');
  return answer;
}

function splitForVk(text) {
  const prefix = env('VK_REPLY_PREFIX', '');
  let remaining = `${prefix}${text || ''}`.trim() || 'Готово.';
  const chunks = [];
  while (remaining.length > VK_MAX_MESSAGE_CHARS) {
    let cut = remaining.lastIndexOf('\n\n', VK_MAX_MESSAGE_CHARS);
    if (cut < VK_MAX_MESSAGE_CHARS / 2) cut = remaining.lastIndexOf('\n', VK_MAX_MESSAGE_CHARS);
    if (cut < VK_MAX_MESSAGE_CHARS / 2) cut = remaining.lastIndexOf(' ', VK_MAX_MESSAGE_CHARS);
    if (cut <= 0) cut = VK_MAX_MESSAGE_CHARS;
    chunks.push(remaining.slice(0, cut).trim());
    remaining = remaining.slice(cut).trim();
  }
  if (remaining) chunks.push(remaining);
  return chunks;
}

async function sendVkMessage(peerId, message) {
  const token = env('VK_GROUP_TOKEN');
  if (!token) throw new Error('VK_GROUP_TOKEN is required');

  const params = new URLSearchParams();
  params.set('access_token', token);
  params.set('v', VK_API_VERSION);
  params.set('peer_id', String(peerId));
  params.set('random_id', String(Math.floor(Math.random() * 2147483647)));
  params.set('message', message);

  const res = await fetch('https://api.vk.com/method/messages.send', {
    method: 'POST',
    headers: { 'content-type': 'application/x-www-form-urlencoded' },
    body: params,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok || data.error) {
    throw new Error(`VK messages.send failed: HTTP ${res.status} ${JSON.stringify(data.error || data).slice(0, 500)}`);
  }
  return data;
}

async function replyVk(peerId, text) {
  for (const chunk of splitForVk(text)) {
    await sendVkMessage(peerId, chunk);
  }
}

async function processMessage(payload) {
  const vk = normalizeVkMessage(payload);
  if (!vk.peerId) return;
  if (vk.message && vk.message.out) return;

  if (isHelpCommand(vk.text)) {
    await replyVk(vk.peerId, helpText());
    return;
  }

  try {
    const answer = await callHermes(vk);
    await replyVk(vk.peerId, answer);
  } catch (err) {
    console.error('VK-Hermes processing failed:', err && err.stack ? err.stack : err);
    await replyVk(vk.peerId, 'Не смог получить ответ от Hermes. Ошибка уже записана в лог функции.');
  }
}

function getSqsClient() {
  if (sqsClient) return sqsClient;
  sqsClient = new SQSClient({
    region: env('AWS_REGION', 'ru-central1'),
    endpoint: env('QUEUE_ENDPOINT', 'https://message-queue.api.cloud.yandex.net'),
  });
  return sqsClient;
}

function queueDedupId(payload) {
  const vk = normalizeVkMessage(payload);
  const raw = [payload.event_id || '', payload.group_id || '', vk.peerId, vk.messageId, vk.text].join('|');
  return crypto.createHash('sha256').update(raw).digest('hex');
}

async function enqueueMessage(payload) {
  const queueUrl = env('QUEUE_URL');
  if (!queueUrl) throw new Error('QUEUE_URL is required in BRIDGE_MODE=queue');

  const body = JSON.stringify({ payload, received_at: new Date().toISOString() });
  const input = {
    QueueUrl: queueUrl,
    MessageBody: body,
  };

  // Yandex Message Queue supports FIFO queues too; set these only for .fifo URLs.
  if (/\.fifo(?:$|[/?#])/.test(queueUrl)) {
    const vk = normalizeVkMessage(payload);
    input.MessageGroupId = vk.peerId || String(payload.group_id || 'vk');
    input.MessageDeduplicationId = queueDedupId(payload);
  }

  await getSqsClient().send(new SendMessageCommand(input));
}

async function invokeInternalProcessor(payload) {
  const url = env('BRIDGE_INTERNAL_URL');
  const secret = env('BRIDGE_INTERNAL_SECRET');
  if (!url || !secret) return false;

  const body = JSON.stringify({ _bridge_internal: true, payload });
  try {
    await fetchWithTimeout(url, {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        'x-bridge-secret': secret,
      },
      body,
    }, BRIDGE_ENQUEUE_TIMEOUT_MS);
    return true;
  } catch (err) {
    console.error('Internal processor invoke did not finish before ACK:', err && err.message ? err.message : err);
    return true;
  }
}

exports.handler = async function handler(event, _context) {
  const payload = parseBody(event);
  if (payload === null) return response(400, 'bad json');

  if (payload && payload._bridge_internal) {
    const expected = env('BRIDGE_INTERNAL_SECRET');
    if (expected && !timingSafeEqualString(header(event, 'x-bridge-secret'), expected)) {
      return response(403, 'invalid internal secret');
    }
    await processMessage(payload.payload || {});
    return response(200, 'ok');
  }

  const validation = validateVkPayload(payload);
  if (!validation.ok) return response(validation.statusCode, validation.body);

  if (payload.type === 'confirmation') {
    return response(200, env('VK_CONFIRMATION_TOKEN'));
  }

  if (payload.type === 'message_new') {
    const mode = env('BRIDGE_MODE', 'queue');
    if (mode === 'queue') {
      try {
        await enqueueMessage(payload);
      } catch (err) {
        console.error('Queue enqueue failed:', err && err.stack ? err.stack : err);
        // Non-2xx forces VK retry instead of silently dropping the event.
        return response(500, 'enqueue failed');
      }
    } else {
      const invoked = await invokeInternalProcessor(payload);
      if (!invoked) {
        if (mode === 'fire_and_forget') {
          processMessage(payload).catch((err) => console.error('Background processing failed:', err));
        } else {
          await processMessage(payload);
        }
      }
    }
  }

  // VK requires literal "ok" for regular events.
  return response(200, 'ok');
};
