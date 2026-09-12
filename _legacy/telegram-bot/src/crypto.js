const encoder = new TextEncoder();
const decoder = new TextDecoder();

function base64ToBytes(value) {
  const normalized = String(value || '').trim();
  if (!normalized) throw new Error('The KV_ENCRYPTION_KEY secret is missing.');
  const binary = atob(normalized);
  return Uint8Array.from(binary, (char) => char.charCodeAt(0));
}

function bytesToBase64(bytes) {
  let binary = '';
  const chunkSize = 0x8000;
  for (let offset = 0; offset < bytes.length; offset += chunkSize) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + chunkSize));
  }
  return btoa(binary);
}

async function importEncryptionKey(secret) {
  const raw = base64ToBytes(secret);
  if (raw.byteLength !== 32) {
    throw new Error('KV_ENCRYPTION_KEY must decode to exactly 32 bytes.');
  }
  return crypto.subtle.importKey('raw', raw, { name: 'AES-GCM' }, false, ['encrypt', 'decrypt']);
}

function aadFor(chatId) {
  return encoder.encode(`clipforge-telegram-credentials:v1:${String(chatId)}`);
}

function relayAad(jobId) {
  return encoder.encode(`clipforge-telegram-relay:v1:${String(jobId)}`);
}

async function encryptObject(plainObject, keySecret, aad) {
  const key = await importEncryptionKey(keySecret);
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const ciphertext = await crypto.subtle.encrypt({ name: 'AES-GCM', iv, additionalData: aad, tagLength: 128 }, key, encoder.encode(JSON.stringify(plainObject)));
  return JSON.stringify({ v: 1, iv: bytesToBase64(iv), ciphertext: bytesToBase64(new Uint8Array(ciphertext)) });
}

export async function encryptCredentials(plainObject, chatId, encryptionSecret) {
  return encryptObject(plainObject, encryptionSecret, aadFor(chatId));
}

// The relay payload is independently encrypted for the trusted central
// workflow. Its GitHub workflow_dispatch input is safe to persist because it
// is authenticated ciphertext, not a clone PAT or Telegram credential.
export async function encryptRelayPayload(plainObject, jobId, encryptionSecret) {
  return encryptObject(plainObject, encryptionSecret, relayAad(jobId));
}

export async function decryptCredentials(serialized, chatId, encryptionSecret) {
  if (!serialized) return null;
  let envelope;
  try {
    envelope = JSON.parse(serialized);
  } catch {
    throw new Error('Stored credentials are unreadable. Reconnect GitHub in /settings.');
  }
  if (!envelope || envelope.v !== 1 || !envelope.iv || !envelope.ciphertext) {
    throw new Error('Stored credentials use an unsupported format. Reconnect GitHub in /settings.');
  }
  const key = await importEncryptionKey(encryptionSecret);
  try {
    const plaintext = await crypto.subtle.decrypt(
      { name: 'AES-GCM', iv: base64ToBytes(envelope.iv), additionalData: aadFor(chatId), tagLength: 128 },
      key,
      base64ToBytes(envelope.ciphertext)
    );
    const parsed = JSON.parse(decoder.decode(plaintext));
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('Invalid credential record.');
    return parsed;
  } catch {
    throw new Error('Stored credentials could not be authenticated. Reconnect GitHub in /settings.');
  }
}

export function maskSecret(value) {
  const raw = String(value || '').trim();
  if (raw.length < 9) return 'configured';
  return `${raw.slice(0, 4)}…${raw.slice(-4)}`;
}
