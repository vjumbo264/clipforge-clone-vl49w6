#!/usr/bin/env node
'use strict';

const assert = require('assert');
const fs = require('fs');
const path = require('path');
const torrent = require('../torrent_metadata.js');

function bytes(text) { return Buffer.from(text, 'utf8'); }
function bstr(text) { return Buffer.concat([bytes(String(Buffer.byteLength(text, 'utf8')) + ':'), bytes(text)]); }
function bint(value) { return bytes('i' + String(value) + 'e'); }
function blist(values) { return Buffer.concat([bytes('l'), ...values, bytes('e')]); }
function bdict(entries) {
  const parts = [bytes('d')];
  entries.forEach(([key, value]) => { parts.push(bstr(key), value); });
  parts.push(bytes('e'));
  return Buffer.concat(parts);
}
function fileEntry(name, length) {
  return bdict([['length', bint(length)], ['path', blist([bstr(name)])]]);
}

const multiTorrent = bdict([['info', bdict([
  ['files', blist([
    fileEntry('episode-01.mkv', 2000),
    fileEntry('episode-02.mkv', 1900),
    fileEntry('trailer.mp4', 100),
    fileEntry('notes.txt', 10),
  ])],
  ['name', bstr('season-pack')],
])]]);
const multi = torrent.inspect(new Uint8Array(multiTorrent));
assert.deepStrictEqual(
  multi.video_candidates.map((entry) => [entry.index, entry.path]),
  [[1, 'episode-01.mkv'], [2, 'episode-02.mkv'], [3, 'trailer.mp4']]
);
assert.strictEqual(multi.video_candidates[1].length, 1900);

const app = fs.readFileSync(path.join(__dirname, '..', 'app.js'), 'utf8');
assert(app.includes("state.torrentVideoIndex = candidates.length === 1 ? candidates[0].index : null;"));
assert(app.includes('createPendingTorrentSelection'));
assert(app.includes('dispatchPendingTorrentSelection'));
assert(app.includes('awaiting_torrent_selection'));
console.log('PASS: browser torrent parser lists multiple candidates and persists explicit selection before Stage A');
