/**
 * Stateless flow wire format — kv-minimization phase 5 (ARCHITECTURE.md §8.9).
 * PURE module: no env, no I/O, no Telegram API — safe to unit-test offline.
 *
 * Bot A keeps NO per-chat navigation record in any datastore. The current
 * step of an input flow travels inside the bot's OWN prompt message as an
 * invisible text_link entity whose URL carries the payload:
 *
 *   <a href="https://cf.invalid/f/cf:<op>:<arg1>:<arg2>">&ZeroWidthSpace;</a>
 *
 * - Reply flows (free-text input): the prompt is sent with force_reply and
 *   the user's next message is matched by parseFlowReply(), which reads
 *   message.reply_to_message (the contract's reply_to/force_reply
 *   mechanism) instead of a stored state row.
 * - Button flows (wizard steps, the music-upload batch): the SAME link rides
 *   in the keyboard message's own text, so callback_data stays tiny and the
 *   callback handler reads its state back out of callback.message.
 *
 * The `.invalid` TLD is reserved (RFC 2606): the link never resolves and is
 * never rendered — only the zero-width space anchor exists in the text.
 *
 * Payload grammar: 'cf:' op (':' arg)*
 *   op   — [a-z][a-z0-9]{1,11}
 *   args — may not contain ':' (base64url / task-label / field-name
 *          alphabets only; enforced by makePayload). There is NO 64-byte
 *          limit here: that limit applies to callback_data (unchanged by
 *          this design), while message text allows ~4 KB.
 *
 * SECURITY: payloads are visible to the chat owner and can be re-injected
 * (a user may quote a marker into a message of their own). Every handler
 * must therefore treat decoded args as untrusted user input: shape-validate
 * (see decodeWizardToken in wizard.js) and keep the commit-boundary gates
 * (e.g. the §9.1 clone gate re-check in startJob) in place.
 */

const FLOW_LINK_PREFIX = 'https://cf.invalid/f/';
const ZWSP = '​';
const OP_RE = /^[a-z][a-z0-9]{1,11}$/;
const ARG_RE = /^[A-Za-z0-9._~-]*$/;

// --- base64url JSON tokens (Workers-safe: btoa/atob + TextEncoder) ------- //

function bytesToB64Url(bytes) {
  let binary = '';
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replaceAll('+', '-').replaceAll('/', '_').replace(/=+$/, '');
}

function b64UrlToBytes(token) {
  const b64 = String(token || '').replaceAll('-', '+').replaceAll('_', '/');
  const binary = atob(b64 + '='.repeat((4 - (b64.length % 4)) % 4));
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
  return bytes;
}

/** JSON -> opaque base64url token (no ':' in the output alphabet). */
export function encodeToken(value) {
  return bytesToB64Url(new TextEncoder().encode(JSON.stringify(value)));
}

/** base64url token -> parsed JSON value, or null on any malformed input. */
export function decodeToken(token) {
  try {
    const value = JSON.parse(new TextDecoder().decode(b64UrlToBytes(token)));
    return value && typeof value === 'object' ? value : null;
  } catch {
    return null;
  }
}

// --- payload codec ------------------------------------------------------- //

/** Build 'cf:<op>:<arg…>' with the wire-format invariants enforced. */
export function makePayload(op, ...args) {
  const name = String(op || '');
  if (!OP_RE.test(name)) throw new Error(`invalid flow opcode: ${name}`);
  for (const arg of args) {
    if (!ARG_RE.test(String(arg))) throw new Error(`invalid flow payload argument for ${name}`);
  }
  return ['cf', name, ...args.map((arg) => String(arg))].join(':');
}

/** Parse a payload string back into { op, args } — null when malformed. */
export function parsePayload(payload) {
  const parts = String(payload || '').split(':');
  if (parts.length < 2 || parts[0] !== 'cf') return null;
  const op = parts[1];
  if (!OP_RE.test(op)) return null;
  const args = parts.slice(2);
  if (!args.every((arg) => ARG_RE.test(arg))) return null;
  return { op, args };
}

// --- message embedding / extraction -------------------------------------- //

/** The invisible anchor appended to a prompt/keyboard message's text. */
export function flowMarkerHtml(payload) {
  return `<a href="${FLOW_LINK_PREFIX}${encodeURIComponent(payload)}">${ZWSP}</a>`;
}

/** Message text + trailing invisible flow marker. */
export function withFlowMarker(text, payload) {
  return `${text}\n${flowMarkerHtml(payload)}`;
}

/**
 * Read the flow payload out of a message the BOT sent (a prompt being
 * replied to, or the keyboard message behind a callback). Returns the raw
 * payload string or null. Only text_link entities are considered.
 */
export function extractFlowPayload(message) {
  const entities = message && Array.isArray(message.entities) ? message.entities : [];
  for (const entity of entities) {
    if (entity && entity.type === 'text_link' && typeof entity.url === 'string' && entity.url.startsWith(FLOW_LINK_PREFIX)) {
      try {
        return decodeURIComponent(entity.url.slice(FLOW_LINK_PREFIX.length));
      } catch {
        return null;
      }
    }
  }
  return null;
}

/** Parse the flow marker on one of the bot's own messages. */
export function parseFlowMessage(message) {
  const parsed = parsePayload(extractFlowPayload(message));
  if (!parsed) return null;
  const messageId = Number(message && message.message_id);
  return {
    ...parsed,
    messageId: Number.isSafeInteger(messageId) && messageId > 0 ? messageId : null
  };
}

/**
 * The reply_to/force_reply gate: returns { op, args, messageId } only when
 * the message replies to one of the BOT's OWN marker-bearing prompts.
 * Anything else (no reply, reply to a human, reply to a foreign bot,
 * marker-less message) returns null and falls through to the home menu.
 */
export function parseFlowReply(message) {
  const target = message && message.reply_to_message;
  if (!target || !target.from || target.from.is_bot !== true) return null;
  return parseFlowMessage(target);
}
