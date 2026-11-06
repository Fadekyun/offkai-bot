import assert from 'node:assert/strict'
import test from 'node:test'
import fs from 'node:fs'
import path from 'node:path'
import { pathToFileURL } from 'node:url'
import ts from 'typescript'

type Element = { props: Record<string, unknown> & { children?: unknown }; type: unknown }

function collect(node: unknown, found: Element[] = []): Element[] {
  if (Array.isArray(node)) { for (const child of node) collect(child, found); return found }
  if (node && typeof node === 'object' && 'props' in node) {
    const element = node as Element
    found.push(element)
    collect(element.props.children, found)
  }
  return found
}

function find(node: unknown, predicate: (element: Element) => boolean): Element | undefined {
  return collect(node).find(predicate)
}

test('edit-names dialog keeps a local draft, snapshots the original registration and reports truthful outcomes', async () => {
  const dir = fs.mkdtempSync(path.join(process.cwd(), 'node_modules', '.edit-names-'))
  const previousDocument = globalThis.document
  try {
    globalThis.document = { addEventListener() {}, removeEventListener() {} } as unknown as Document
    fs.writeFileSync(path.join(dir, 'package.json'), '{"type":"module"}')
    fs.writeFileSync(path.join(dir, 'react.js'), `const states=[],refs=[],effectDeps=[],cleanups=[];
      let index=0,refIndex=0,effects=[];
      export function begin(){index=0;refIndex=0;effects=[];}
      export function useState(initial){const key=index++;if(!(key in states))states[key]=initial;
        return [states[key],value=>{states[key]=typeof value==='function'?value(states[key]):value}];}
      export function useRef(initial){const key=refIndex++;if(!(key in refs))refs[key]={current:initial};
        return refs[key];}
      export function useEffect(fn,deps){effects.push({fn,deps});}
      export function runEffects(){effects.forEach((effect,key)=>{
        const previous=effectDeps[key];
        const changed=!previous||!effect.deps||effect.deps.length!==previous.length||
          effect.deps.some((value,i)=>!Object.is(value,previous[i]));
        if(!changed)return;
        if(cleanups[key])cleanups[key]();
        cleanups[key]=effect.fn();
        effectDeps[key]=effect.deps;
      });}
      export function reset(){states.length=0;refs.length=0;effectDeps.length=0;cleanups.length=0;
        index=0;refIndex=0;effects=[];}`)
    fs.writeFileSync(path.join(dir, 'i18n.js'), `export function useT(){return {t:{
      editNames:'Edit names',attendeeName:'Attendee name',guestName:n=>'Guest '+n+' name',
      unnamedGuest:'Unnamed guest',saveNames:'Save names',cancel:'Cancel',namesSaved:'Names saved.',
      namesNoChanges:'No changes to save.',namesInvalid:'Invalid names',
      namesConflict:'Registration changed',namesUnconfirmed:'Unconfirmed',namesBusy:'Too busy',namesExpired:'Session expired',namesSaveError:'Save error',
      namesTooLarge:'Edit too large'}};}`)
    const code = ts.transpileModule(fs.readFileSync('app/components/admin/EditNames.tsx', 'utf8'), {
      compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX },
    }).outputText.replaceAll("from 'react'", "from './react.js'").replaceAll("from '../../lib/i18n'", "from './i18n.js'")
    fs.writeFileSync(path.join(dir, 'component.js'), code)
    const { begin, reset, runEffects } = await import(pathToFileURL(path.join(dir, 'react.js')).href)
    const { EditNames } = await import(pathToFileURL(path.join(dir, 'component.js')).href)

    const attendee = {
      user_id: '191524132624531458', username: 'synthetic', display_name: 'Before',
      drinks: [], extra_people: 2, extras_names: ['Alice'], attendee_number: 1,
      extras_attendee_numbers: [2, 3], status: 'attending', registration_timestamp: '2026-10-01T00:00:00.123Z',
      names_revision: 'a'.repeat(64),
    }
    const calls: unknown[][] = []
    let outcome = 'saved'
    let saveHandler = async (...args: unknown[]) => { calls.push(args); return outcome }
    const render = (overrides = {}) => {
      begin()
      const tree = EditNames({ attendee: { ...attendee, ...overrides }, eventName: 'Test event', onSave: saveHandler })
      runEffects()
      return tree
    }
    const openWith = (overrides = {}) => {
      const button = find(render(overrides), element => element.props['aria-label'] === 'Edit names')
      assert.ok(button)
      ;(button!.props.onClick as () => void)()
      return render(overrides)
    }

    let tree = openWith()
    let dialog = find(tree, element => element.props.role === 'dialog')
    assert.ok(dialog)
    let inputs = collect(dialog).filter(element => element.type === 'input')
    assert.equal(inputs.length, 3)
    assert.equal(inputs[1].props.value, 'Alice')
    assert.equal(inputs[2].props.value, '')
    assert.equal(inputs[2].props.placeholder, 'Unnamed guest')

    // Polling may refresh the row, but it must not touch the open draft or its snapshot.
    ;(inputs[0].props.onChange as (event: unknown) => void)({ target: { value: ' Draft ' } })
    tree = render({ display_name: 'Changed by polling' })
    dialog = find(tree, element => element.props.role === 'dialog')
    inputs = collect(dialog).filter(element => element.type === 'input')
    assert.equal(inputs[0].props.value, ' Draft ')
    const form = find(dialog, element => element.type === 'form')
    await (form!.props.onSubmit as (event: unknown) => Promise<void>)({ preventDefault() {} })
    assert.deepEqual(calls[0], [
      'Test event', attendee.user_id, attendee.registration_timestamp, 'a'.repeat(64),
      { display_name: 'Draft' },
    ])
    tree = render()
    assert.equal(find(tree, element => element.props.role === 'dialog'), undefined)
    assert.equal(find(tree, element => element.props.role === 'status')?.props.children, 'Names saved.')

    // A conflict or ambiguous result closes the stale editor instead of claiming success.
    for (const [failure, expected] of [['conflict', 'Registration changed'], ['unconfirmed', 'Unconfirmed']] as const) {
      outcome = failure
      tree = openWith()
      dialog = find(tree, element => element.props.role === 'dialog')
      inputs = collect(dialog).filter(element => element.type === 'input')
      ;(inputs[0].props.onChange as (event: unknown) => void)({ target: { value: 'New' } })
      tree = render()
      dialog = find(tree, element => element.props.role === 'dialog')
      const failureForm = find(dialog, element => element.type === 'form')
      await (failureForm!.props.onSubmit as (event: unknown) => Promise<void>)({ preventDefault() {} })
      tree = render()
      assert.equal(find(tree, element => element.props.role === 'dialog'), undefined)
      assert.equal(find(tree, element => element.props.role === 'alert')?.props.children, expected)
    }

    // Busy and expired sessions keep the draft with clear localized errors.
    for (const [failure, expected] of [['busy', 'Too busy'], ['expired', 'Session expired']] as const) {
      const beforeBusy = calls.length
      outcome = failure
      tree = openWith()
      dialog = find(tree, element => element.props.role === 'dialog')
      inputs = collect(dialog).filter(element => element.type === 'input')
      ;(inputs[0].props.onChange as (event: unknown) => void)({ target: { value: 'Busy draft' } })
      tree = render()
      dialog = find(tree, element => element.props.role === 'dialog')
      const failureForm = find(dialog, element => element.type === 'form')
      await (failureForm!.props.onSubmit as (event: unknown) => Promise<void>)({ preventDefault() {} })
      tree = render()
      dialog = find(tree, element => element.props.role === 'dialog')
      assert.ok(dialog)
      assert.equal(find(dialog, element => element.props.role === 'alert')?.props.children, expected)
      assert.equal(calls.length, beforeBusy + 1)
    }

    // A known too-large rejection keeps the draft and shows the localized size message.
    outcome = 'too_large'
    const beforeTooLarge = calls.length
    tree = openWith()
    dialog = find(tree, element => element.props.role === 'dialog')
    inputs = collect(dialog).filter(element => element.type === 'input')
    ;(inputs[0].props.onChange as (event: unknown) => void)({ target: { value: 'Too large draft' } })
    tree = render()
    dialog = find(tree, element => element.props.role === 'dialog')
    const tooLargeForm = find(dialog, element => element.type === 'form')
    await (tooLargeForm!.props.onSubmit as (event: unknown) => Promise<void>)({ preventDefault() {} })
    tree = render()
    dialog = find(tree, element => element.props.role === 'dialog')
    assert.ok(dialog)
    assert.equal(find(dialog, element => element.props.role === 'alert')?.props.children, 'Edit too large')
    assert.equal(calls.length, beforeTooLarge + 1)
    assert.equal(find(tree, element => element.props.role === 'status'), undefined)

    // Blank edits and a no-op save never call the bot.
    outcome = 'saved'
    const before = calls.length
    tree = openWith()
    dialog = find(tree, element => element.props.role === 'dialog')
    const noChangeForm = find(dialog, element => element.type === 'form')
    await (noChangeForm!.props.onSubmit as (event: unknown) => Promise<void>)({ preventDefault() {} })
    tree = render()
    assert.equal(calls.length, before)
    assert.equal(find(tree, element => element.props.role === 'alert')?.props.children, 'No changes to save.')
    tree = openWith()
    dialog = find(tree, element => element.props.role === 'dialog')
    inputs = collect(dialog).filter(element => element.type === 'input')
    ;(inputs[0].props.onChange as (event: unknown) => void)({ target: { value: '   ' } })
    tree = render()
    dialog = find(tree, element => element.props.role === 'dialog')
    const blankForm = find(dialog, element => element.type === 'form')
    await (blankForm!.props.onSubmit as (event: unknown) => Promise<void>)({ preventDefault() {} })
    assert.equal(calls.length, before)
    assert.equal(find(render(), element => element.props.role === 'alert')?.props.children, 'Invalid names')

    // Identity replacement closes the editor so an old snapshot cannot be submitted.
    tree = openWith()
    tree = render({ registration_timestamp: '2026-10-02T00:00:00.000Z' })
    assert.equal(find(tree, element => element.props.role === 'dialog'), undefined)

    // Zero-guest registrations show no guest inputs.
    tree = openWith({ extra_people: 0, extras_names: [] })
    dialog = find(tree, element => element.props.role === 'dialog')
    inputs = collect(dialog).filter(element => element.type === 'input')
    assert.equal(inputs.length, 1)

    // Five-guest registrations expose exactly the existing five slots.
    tree = openWith({ extra_people: 5, extras_names: ['A', 'B', 'C', 'D', 'E'] })
    dialog = find(tree, element => element.props.role === 'dialog')
    inputs = collect(dialog).filter(element => element.type === 'input')
    assert.equal(inputs.length, 6)
    assert.deepEqual(inputs.slice(1).map(input => input.props.value), ['A', 'B', 'C', 'D', 'E'])

    // A legacy attendee record may omit display_name entirely. Capturing null keeps
    // the snapshot present on the wire while leaving both display-name inputs untouched.
    const legacy = { display_name: undefined, extra_people: 1, extras_names: ['Legacy Guest'],
      extras_attendee_numbers: [2], names_revision: 'b'.repeat(64) }
    const beforeLegacy = calls.length
    tree = openWith(legacy)
    dialog = find(tree, element => element.props.role === 'dialog')
    inputs = collect(dialog).filter(element => element.type === 'input')
    ;(inputs[1].props.onChange as (event: unknown) => void)({ target: { value: ' Legacy Updated ' } })
    tree = render(legacy)
    dialog = find(tree, element => element.props.role === 'dialog')
    const legacyForm = find(dialog, element => element.type === 'form')
    await (legacyForm!.props.onSubmit as (event: unknown) => Promise<void>)({ preventDefault() {} })
    assert.equal(calls.length, beforeLegacy + 1)
    assert.equal(calls.at(-1)![3], 'b'.repeat(64))
    const legacyChanges = calls.at(-1)![4] as Record<string, unknown>
    assert.deepEqual(legacyChanges, { extras_names: [{ index: 0, value: 'Legacy Updated' }] })
    assert.equal(Object.hasOwn(legacyChanges, 'display_name'), false)

    // Cancelling closes the editor without calling the bot or showing a result.
    const beforeCancel = calls.length
    tree = openWith()
    dialog = find(tree, element => element.props.role === 'dialog')
    const cancel = find(dialog, element => element.type === 'button' && element.props.children === 'Cancel')
    assert.ok(cancel)
    ;(cancel!.props.onClick as () => void)()
    tree = render()
    assert.equal(find(tree, element => element.props.role === 'dialog'), undefined)
    assert.equal(find(tree, element => element.props.role === 'status'), undefined)
    assert.equal(find(tree, element => element.props.role === 'alert'), undefined)
    assert.equal(calls.length, beforeCancel)

    // A completion for an old registration cannot leak into a replacement's editor.
    let resolvePending!: (value: string) => void
    saveHandler = (() => new Promise<string>(resolve => { resolvePending = resolve })) as typeof saveHandler
    tree = openWith()
    dialog = find(tree, element => element.props.role === 'dialog')
    inputs = collect(dialog).filter(element => element.type === 'input')
    ;(inputs[0].props.onChange as (event: unknown) => void)({ target: { value: 'Old request' } })
    tree = render()
    dialog = find(tree, element => element.props.role === 'dialog')
    const pendingForm = find(dialog, element => element.type === 'form')
    const pendingSubmit = (pendingForm!.props.onSubmit as (event: unknown) => Promise<void>)({ preventDefault() {} })
    tree = render({ registration_timestamp: '2026-10-02T00:00:00.000Z', display_name: 'Rejoined' })
    resolvePending('saved')
    await pendingSubmit
    tree = render({ registration_timestamp: '2026-10-02T00:00:00.000Z', display_name: 'Rejoined' })
    assert.equal(find(tree, element => element.props.role === 'dialog'), undefined)
    assert.equal(find(tree, element => element.props.role === 'status'), undefined)
    assert.equal(find(tree, element => element.props.role === 'alert'), undefined)
    tree = openWith({ registration_timestamp: '2026-10-02T00:00:00.000Z', display_name: 'Rejoined' })
    dialog = find(tree, element => element.props.role === 'dialog')
    inputs = collect(dialog).filter(element => element.type === 'input')
    assert.equal(inputs[0].props.value, 'Rejoined')
    reset()
  } finally {
    if (previousDocument === undefined) delete (globalThis as { document?: Document }).document
    else globalThis.document = previousDocument
    fs.rmSync(dir, { recursive: true, force: true })
  }
})
