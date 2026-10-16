import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { crc32, exportToZip, isZip, storeZip, zipEntryNames } from '../src/shared/zip.js';

const enc = new TextEncoder();

test('crc32 matches the standard check value', () => {
  assert.equal(crc32(enc.encode('123456789')), 0xcbf43926);
});

test('a stored zip lists its entries and is readable by Python zipfile', (t) => {
  const text = enc.encode('[6/17/25, 3:45:12 PM] Ana Prueba: Hola ñandú\n');
  const zip = storeZip([{ name: '_chat.txt', bytes: text }]);
  assert.ok(isZip(zip));
  assert.deepEqual(zipEntryNames(zip), ['_chat.txt']);
  // The server reads it with Python's zipfile: cross-check when available.
  const path = join(mkdtempSync(join(tmpdir(), 'n16zip-')), 'chat.zip');
  writeFileSync(path, zip);
  const script =
    'import sys, zipfile\n' +
    'z = zipfile.ZipFile(sys.argv[1])\n' +
    'assert z.testzip() is None\n' +
    'sys.stdout.write(z.read("_chat.txt").decode())';
  let out;
  try {
    out = execFileSync('python3', ['-I', '-c', script, path], { encoding: 'utf8' });
  } catch (err) {
    if (err.code === 'ENOENT') return t.skip('python3 not available');
    throw err;
  }
  assert.equal(out, new TextDecoder().decode(text));
});

test('exportToZip wraps bare text and keeps a WhatsApp zip', () => {
  const wrapped = exportToZip(enc.encode('hola'));
  assert.deepEqual(zipEntryNames(wrapped), ['_chat.txt']);
  assert.equal(exportToZip(wrapped), wrapped);
  const noChat = storeZip([{ name: 'IMG-1.jpg', bytes: enc.encode('x') }]);
  assert.throws(() => exportToZip(noChat), /export_zip_without_txt/);
});
