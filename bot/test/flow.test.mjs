/**
 * flow.js — the stateless flow wire format (kv-minimization phase 5).
 * These tests pin the marker grammar, the base64url token codec, and the
 * reply_to/force_reply gate that replaces stored chat state.
 */

import test from 'node:test';
import assert from 'node:assert/strict';
import {
  encodeToken, decodeToken, makePayload, parsePayload,
  flowMarkerHtml, withFlowMarker, extractFlowPayload, parseFlowMessage, parseFlowReply
} from '../src/flow.js';

// --- helpers ------------------------------------------------------------- //

function botMessageWithPayload(payload, messageId = 900) {
  const marker = flowMarkerHtml(payload);
  return {
    message_id: messageId,
    from: { is_bot: true, id: 111, first_name: 'ClipForge' },
    text: `Prompt text\n${marker.replace(/<[^>]+>/g, '​')}`,
    entities: [
      { type: 'bold', offset: 0, length: 6 },
      { type: 'text_link', offset: 12, length: 1, url: `https://cf.invalid/f/${encodeURIComponent(payload)}` }
    ]
  };
}

// --- token codec --------------------------------------------------------- //

test('encodeToken/decodeToken round-trips objects with unicode + separators', () => {
  const wizard = {
    v: 1, step: 'music', jobId: 'manual-1788089510438', mode: 'manual', series: true,
    source: { kind: 'url', value: 'https://example.com/a:b:c?v=1&x=2' },
    focus: 'the trial — "cross-examination" ❤️', duration: 60,
    music: { ref: 'audio-library/track one.m4a', source: 'explicit_library' }
  };
  const token = encodeToken(wizard);
  assert.ok(!token.includes(':'), 'token must never contain the payload separator');
  assert.ok(!/[=+/]/.test(token), 'token must be base64url, unpadded');
  assert.deepEqual(decodeToken(token), wizard);
});

test('decodeToken returns null on garbage instead of throwing', () => {
  assert.equal(decodeToken(''), null);
  assert.equal(decodeToken('!!!not-base64!!!'), null);
  assert.equal(decodeToken('aGVsbG8'), null); // valid b64, not an object
  assert.equal(decodeToken(null), null);
});

// --- payload grammar ----------------------------------------------------- //

test('makePayload builds and parsePayload round-trips op + args', () => {
  const payload = makePayload('tppm', 'AB', 'post_1788');
  assert.equal(payload, 'cf:tppm:AB:post_1788');
  assert.deepEqual(parsePayload(payload), { op: 'tppm', args: ['AB', 'post_1788'] });
});

test('makePayload enforces the grammar', () => {
  assert.throws(() => makePayload('')); // no opcode
  assert.throws(() => makePayload('A')); // must start lowercase
  assert.throws(() => makePayload('toolongopcode12')); // > 12 chars
  assert.throws(() => makePayload('wz', 'has:colon')); // arg separator
  assert.throws(() => makePayload('wz', 'has space'));
});

test('parsePayload rejects foreign or malformed payloads', () => {
  assert.equal(parsePayload(''), null);
  assert.equal(parsePayload('xx:wz:1'), null);
  assert.equal(parsePayload('cf:'), null);
  assert.equal(parsePayload('cf:OK'), null);
  assert.equal(parsePayload('cf:wz:bad arg'), null);
  assert.deepEqual(parsePayload('cf:clname'), { op: 'clname', args: [] });
});

// --- message embedding / extraction -------------------------------------- //

test('withFlowMarker appends an invisible text_link anchor carrying the payload', () => {
  const payload = makePayload('patnew', encodeToken('my-clone'));
  const text = withFlowMarker('<b>Send the token</b>', payload);
  const message = {
    text: text.replace(/<[^>]+>/g, '​'),
    entities: [{ type: 'text_link', offset: 20, length: 1, url: `https://cf.invalid/f/${encodeURIComponent(payload)}` }]
  };
  assert.equal(extractFlowPayload(message), payload);
  assert.ok(!text.replace(/<[^>]+>/g, '').includes('cf:'), 'payload never appears in visible text');
});

test('extractFlowPayload ignores non-flow links and missing entities', () => {
  assert.equal(extractFlowPayload(null), null);
  assert.equal(extractFlowPayload({}), null);
  assert.equal(extractFlowPayload({ entities: [{ type: 'text_link', url: 'https://example.com/f/cf:wz:1' }] }), null);
  assert.equal(extractFlowPayload({ entities: [{ type: 'url', offset: 0, length: 5 }] }), null);
});

test('parseFlowMessage reports the prompt message id alongside op/args', () => {
  const message = botMessageWithPayload('cf:wz:abc123', 4242);
  assert.deepEqual(parseFlowMessage(message), { op: 'wz', args: ['abc123'], messageId: 4242 });
});

// --- the reply_to / force_reply gate ------------------------------------- //

test('parseFlowReply accepts a reply to the bot\'s own marker prompt', () => {
  const prompt = botMessageWithPayload('cf:zsch:timezone', 777);
  const reply = { message_id: 778, text: 'Europe/London', reply_to_message: prompt };
  assert.deepEqual(parseFlowReply(reply), { op: 'zsch', args: ['timezone'], messageId: 777 });
});

test('parseFlowReply rejects everything that is not a reply to the bot\'s own prompt', () => {
  const prompt = botMessageWithPayload('cf:zsch:timezone', 777);
  // no reply at all
  assert.equal(parseFlowReply({ message_id: 1, text: 'UTC' }), null);
  // reply to a human's message (even one quoting a marker-looking link)
  const humanMessage = { ...prompt, from: { is_bot: false, id: 5 } };
  assert.equal(parseFlowReply({ message_id: 2, reply_to_message: humanMessage }), null);
  // reply to a bot message WITHOUT a marker (e.g. the home menu)
  const plain = { message_id: 3, from: { is_bot: true }, text: 'menu', entities: [] };
  assert.equal(parseFlowReply({ message_id: 4, reply_to_message: plain }), null);
  // reply to a bot message with a MALFORMED payload
  const bad = botMessageWithPayload('cf:BAD OP', 9);
  assert.equal(parseFlowReply({ message_id: 5, reply_to_message: bad }), null);
});

// --- production.json upload payloads (kv-minimization phase 5 step 5.3) --- //

test('upl payload carries just the task label and round-trips through parseFlowReply', () => {
  const label = 'AB';
  const payload = makePayload('upl', label);
  assert.equal(payload, 'cf:upl:AB');
  const prompt = botMessageWithPayload(payload, 501);
  const reply = { message_id: 502, text: '{"parts":[]}', reply_to_message: prompt };
  assert.deepEqual(parseFlowReply(reply), { op: 'upl', args: [label], messageId: 501 });
});

// --- onboarding payloads (kv-minimization phase 5 step 5.4) -------------- //

test('clname / patc / repo payloads are arg-less and route by opcode alone', () => {
  for (const op of ['clname', 'patc', 'repo']) {
    const payload = makePayload(op);
    assert.equal(payload, `cf:${op}`);
    const prompt = botMessageWithPayload(payload, 700);
    const reply = { message_id: 701, text: 'answer', reply_to_message: prompt };
    assert.deepEqual(parseFlowReply(reply), { op, args: [], messageId: 700 });
  }
});

test('patnew carries the clone name as a b64url { n } token; empty n = auto-name', () => {
  // decodeToken only returns plain OBJECTS (strings collapse to null by
  // design), so the name rides as { n: name } — the one-field record pattern.
  const named = makePayload('patnew', encodeToken({ n: 'my-clipforge.v2' }));
  const parsedNamed = parseFlowReply({ message_id: 801, text: 'ghp_x', reply_to_message: botMessageWithPayload(named, 800) });
  assert.equal(parsedNamed.op, 'patnew');
  assert.deepEqual(decodeToken(parsedNamed.args[0]), { n: 'my-clipforge.v2' });
  // bug-45 auto-name sentinel: { n: '' } must keep n as the empty string.
  const auto = makePayload('patnew', encodeToken({ n: '' }));
  const parsedAuto = parseFlowReply({ message_id: 901, text: 'ghp_x', reply_to_message: botMessageWithPayload(auto, 900) });
  const autoArg = decodeToken(parsedAuto.args[0]);
  assert.equal(autoArg && typeof autoArg.n === 'string' ? autoArg.n : null, '');
});

test('patnew with a garbage name token decodes to null so the handler fails closed', () => {
  const payload = makePayload('patnew', 'not-a-real-token');
  const parsed = parseFlowReply({ message_id: 2, text: 'ghp_x', reply_to_message: botMessageWithPayload(payload, 1) });
  assert.equal(parsed.op, 'patnew');
  assert.equal(decodeToken(parsed.args[0]), null); // handler re-asks for the name
});

// --- settings-input payloads (kv-minimization phase 5 step 5.5) ---------- //

test('gemkey / wm / news / zkey payloads are arg-less and route by opcode alone', () => {
  for (const op of ['gemkey', 'wm', 'news', 'zkey']) {
    const payload = makePayload(op);
    assert.equal(payload, `cf:${op}`);
    const reply = { message_id: 951, text: 'answer', reply_to_message: botMessageWithPayload(payload, 950) };
    assert.deepEqual(parseFlowReply(reply), { op, args: [], messageId: 950 });
  }
});

test('zsch payload carries the smart-schedule field name as a bare arg', () => {
  // The field alphabet (timezone/interval/time/depth/custom_start) is ARG_RE-safe.
  for (const field of ['timezone', 'interval', 'time', 'depth', 'custom_start']) {
    const payload = makePayload('zsch', field);
    assert.equal(payload, `cf:zsch:${field}`);
    const reply = { message_id: 961, text: 'x', reply_to_message: botMessageWithPayload(payload, 960) };
    assert.deepEqual(parseFlowReply(reply), { op: 'zsch', args: [field], messageId: 960 });
  }
  // A re-injected zsch marker with an unknown field still PARSES (the handler
  // relies on applySmartScheduleField failing closed, not on the codec).
  const forged = parseFlowReply({ message_id: 971, text: 'x', reply_to_message: botMessageWithPayload(makePayload('zsch', 'evil'), 970) });
  assert.deepEqual(forged, { op: 'zsch', args: ['evil'], messageId: 970 });
});

// --- task publish prompt payloads (kv-minimization phase 5 step 5.6) ----- //

test('tpm carries the task label; tppm carries label + Zernio post id', () => {
  const tpm = makePayload('tpm', 'AB');
  assert.equal(tpm, 'cf:tpm:AB');
  assert.deepEqual(
    parseFlowReply({ message_id: 981, text: '2026-09-01T09:00', reply_to_message: botMessageWithPayload(tpm, 980) }),
    { op: 'tpm', args: ['AB'], messageId: 980 });
  // POST_ID_PATTERN-shaped ids (e.g. post_1788…) are ARG_RE-safe: no escaping.
  const tppm = makePayload('tppm', 'AB', 'post_1788089510438');
  assert.equal(tppm, 'cf:tppm:AB:post_1788089510438');
  assert.deepEqual(
    parseFlowReply({ message_id: 991, text: 'x', reply_to_message: botMessageWithPayload(tppm, 990) }),
    { op: 'tppm', args: ['AB', 'post_1788089510438'], messageId: 990 });
});

// --- music-upload batch payloads (kv-minimization phase 5 step 5.7) ------- //

test('mupl carries the staged {u:[{name,file_id}]} list; Done reads it from the keyboard message', () => {
  const list = { u: [{ name: 'track one.m4a', file_id: 'AAID123' }, { name: 'b.mp3', file_id: 'BBID456' }] };
  const token = encodeToken(list);
  assert.ok(!token.includes(':'), 'list token must never break the payload grammar');
  const payload = makePayload('mupl', token);
  assert.match(payload, /^cf:mupl:[A-Za-z0-9._~-]+$/);
  // Reply path: a replied audio file routes to handleMusicUploadMessage.
  const reply = { message_id: 1002, text: '', reply_to_message: botMessageWithPayload(payload, 1001) };
  const parsed = parseFlowReply(reply);
  assert.equal(parsed.op, 'mupl');
  assert.deepEqual(decodeToken(parsed.args[0]), list);
  // Button path: Done reads the SAME marker off callback.message.
  assert.deepEqual(parseFlowMessage(botMessageWithPayload(payload, 1001)), { op: 'mupl', args: [token], messageId: 1001 });
});

test('mupl with a garbage list token decodes to an empty batch, never throws', () => {
  const payload = makePayload('mupl', 'not-a-real-token');
  const parsed = parseFlowReply({ message_id: 2, text: '', reply_to_message: botMessageWithPayload(payload, 1) });
  assert.equal(parsed.op, 'mupl');
  const decoded = decodeToken(parsed.args[0]);
  assert.equal(decoded, null); // handler defaults to { uploaded: [] }
});

test('every ARG_RE-legal task label survives makePayload without escaping', () => {
  // ensureTaskLabel emits A..Z, AA.., AB.., etc. (nextLabel base-26).
  // Every character is ARG_RE-safe: no marker escaping ever needed.
  for (const label of ['A', 'B', 'Z', 'AA', 'AZ', 'BA', 'ZZ', 'AAA']) {
    assert.doesNotThrow(() => makePayload('upl', label));
  }
});
