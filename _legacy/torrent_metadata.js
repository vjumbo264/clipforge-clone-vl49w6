/* Browser-safe .torrent metadata parser. It never contacts trackers or peers. */
(function (root) {
  'use strict';

  var MAX_TORRENT_BYTES = 1024 * 1024;
  var MAX_VIDEO_BYTES = 12 * 1024 * 1024 * 1024;
  var VIDEO_EXTENSIONS = ['.mkv', '.mp4', '.m4v', '.mov', '.webm', '.avi', '.ts', '.m2ts'];

  function fail(message) { throw new Error('Torrent metadata error: ' + message); }

  function decode(bytes) {
    var at = 0;
    function parse() {
      if (at >= bytes.length) fail('unexpected end of data');
      var token = bytes[at];
      if (token === 105) { // i
        at += 1;
        var integerEnd = at;
        while (integerEnd < bytes.length && bytes[integerEnd] !== 101) integerEnd += 1;
        if (integerEnd >= bytes.length) fail('unterminated integer');
        var integerText = new TextDecoder('ascii').decode(bytes.slice(at, integerEnd));
        if (!/^-?(0|[1-9][0-9]*)$/.test(integerText)) fail('invalid integer');
        at = integerEnd + 1;
        return Number(integerText);
      }
      if (token === 108) { // l
        at += 1;
        var list = [];
        while (at < bytes.length && bytes[at] !== 101) list.push(parse());
        if (at >= bytes.length) fail('unterminated list');
        at += 1;
        return list;
      }
      if (token === 100) { // d
        at += 1;
        var dict = Object.create(null);
        while (at < bytes.length && bytes[at] !== 101) {
          var key = parse();
          if (!(key instanceof Uint8Array)) fail('dictionary key is not bytes');
          dict[new TextDecoder('utf-8', { fatal: false }).decode(key)] = parse();
        }
        if (at >= bytes.length) fail('unterminated dictionary');
        at += 1;
        return dict;
      }
      if (token >= 48 && token <= 57) {
        var colon = at;
        while (colon < bytes.length && bytes[colon] !== 58) colon += 1;
        if (colon >= bytes.length) fail('missing byte-string separator');
        var lengthText = new TextDecoder('ascii').decode(bytes.slice(at, colon));
        if (!/^(0|[1-9][0-9]*)$/.test(lengthText)) fail('invalid byte-string length');
        var length = Number(lengthText);
        at = colon + 1;
        if (at + length > bytes.length) fail('truncated byte string');
        var value = bytes.slice(at, at + length);
        at += length;
        return value;
      }
      fail('unknown bencode token');
    }
    var result = parse();
    if (at !== bytes.length) fail('trailing data');
    return result;
  }

  function text(value, field) {
    if (!(value instanceof Uint8Array)) fail(field + ' is missing or invalid');
    return new TextDecoder('utf-8', { fatal: false }).decode(value);
  }

  function safePart(value, field) {
    if (!value || value === '.' || value === '..' || /[\\/\u0000]/.test(value)) {
      fail(field + ' contains an unsafe path component');
    }
    return value;
  }

  function isVideo(path) {
    var lower = path.toLowerCase();
    return VIDEO_EXTENSIONS.some(function (ext) { return lower.endsWith(ext); });
  }

  function inspect(bytes) {
    if (!(bytes instanceof Uint8Array) || !bytes.length) fail('file is empty');
    if (bytes.length > MAX_TORRENT_BYTES) fail('file exceeds 1 MB limit');
    var rootDict = decode(bytes);
    if (!rootDict || typeof rootDict !== 'object' || Array.isArray(rootDict)) fail('root is invalid');
    var info = rootDict.info;
    if (!info || typeof info !== 'object' || Array.isArray(info)) fail('info dictionary is missing');
    var name = safePart(text(info.name, 'info.name'), 'info.name');
    var files = [];
    if (Array.isArray(info.files)) {
      if (!info.files.length) fail('file list is empty');
      info.files.forEach(function (entry, offset) {
        if (!entry || typeof entry.length !== 'number' || !Array.isArray(entry.path) || !entry.path.length) {
          fail('invalid file entry');
        }
        var parts = entry.path.map(function (part) {
          return safePart(text(part, 'file path'), 'file path');
        });
        files.push({ index: offset + 1, path: parts.join('/'), length: entry.length });
      });
    } else {
      if (typeof info.length !== 'number') fail('single-file length is missing');
      files.push({ index: 1, path: name, length: info.length });
    }
    var candidates = files.filter(function (file) {
      return isVideo(file.path) && file.length > 0 && file.length <= MAX_VIDEO_BYTES;
    });
    if (!candidates.length) fail('no supported video file under the 12 GB limit');
    return {
      name: name,
      file_count: files.length,
      total_bytes: files.reduce(function (total, file) { return total + Math.max(file.length, 0); }, 0),
      video_candidates: candidates
    };
  }

  var api = { inspect: inspect, VIDEO_EXTENSIONS: VIDEO_EXTENSIONS, MAX_TORRENT_BYTES: MAX_TORRENT_BYTES };
  root.ClipForgeTorrent = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
