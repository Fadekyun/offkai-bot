import assert from 'node:assert/strict'
import test from 'node:test'
import fs from 'node:fs'
import path from 'node:path'
import { pathToFileURL } from 'node:url'
import ts from 'typescript'

async function loadNamesRevision() {
  const dir = fs.mkdtempSync(path.join(process.cwd(), 'node_modules', '.names-revision-'))
  fs.writeFileSync(path.join(dir, 'package.json'), '{"type":"module"}')
  const code = ts.transpileModule(fs.readFileSync('app/lib/namesRevision.ts', 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
  }).outputText
  fs.writeFileSync(path.join(dir, 'namesRevision.js'), code)
  const loadedModule = await import(pathToFileURL(path.join(dir, 'namesRevision.js')).href)
  return { namesRevision: loadedModule.namesRevision as (
    displayName: string | null, extraPeople: number, extrasNames: (string | null)[],
  ) => string, cleanup: () => fs.rmSync(dir, { recursive: true, force: true }) }
}

// These hashes are shared with bot/tests/data/test_names_revision.py.
const vectors: [string | null, number, (string | null)[], string][] = [
  [null, 0, [], '6bc5c3cd0b0c09ef7f909772f36e25e70b7fc42e7b9bfddeee2796e5fef7211e'],
  ['', 0, [], 'c418343ea53bb9cd9cd8e7d2ed3ee78c96d0f96c899661be0b2cf2cc196cd5db'],
  ['Before', 2, ['Alice', 'Bob'], '7c709e8979b4d4ec70d3cccf92484a282ae394b60e828a9d37a2d802453bdbef'],
  ['日本語😀', 5, ['客😀', null, '', 'surplus😀'],
    'bb4b4035c05c012473db134ae01e38391b2a51fb78c79270737300da96a3f333'],
  ['\u007f\u0080\u2028\ud83d\ude00', 1, ['\u0000\t\n'],
    '6a53ee2393228323544afecfe0189593d565f6fd437d2dabc3955036b3282218'],
]

test('browser names revision matches the bot canonical SHA-256 vectors', async () => {
  const loaded = await loadNamesRevision()
  try {
    for (const [displayName, extraPeople, extrasNames, expected] of vectors) {
      assert.equal(loaded.namesRevision(displayName, extraPeople, extrasNames), expected)
    }
  } finally {
    loaded.cleanup()
  }
})

test('missing and surplus guest slots stay distinguishable', async () => {
  const { namesRevision, cleanup } = await loadNamesRevision()
  try {
    assert.notEqual(namesRevision(null, 2, ['Alice']), namesRevision(null, 2, ['Alice', '']))
    assert.notEqual(namesRevision(null, 2, ['Alice', 'Bob']),
      namesRevision(null, 2, ['Alice', 'Bob', 'Legacy surplus']))
    assert.notEqual(namesRevision(null, 1, ['Alice']), namesRevision('', 1, ['Alice']))
  } finally {
    cleanup()
  }
})
