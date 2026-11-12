import test from 'node:test'
import assert from 'node:assert/strict'
import {
  capabilityDraft, defaultWebPolicy, effectiveCapability, modelChanged, policyChanged,
  saveSettings, settingsRequest, SettingsSaveError, SettingsValidationError, toggleCapability,
} from '../src/lib/agent-settings.ts'
import type { CapabilityCatalog, SaveSettingsOptions } from '../src/lib/agent-settings.ts'

function fixture() {
  const catalog: CapabilityCatalog = {
    version: 7, catalog_version: 'catalog-v1', disabled_ids: [], enabled_ids: [],
    web_read: structuredClone(defaultWebPolicy), capabilities: [
      { id: 'files', title: '文件', description: '', contexts: [], requires: [], enabled: {} },
      { id: 'pdf', title: 'PDF', description: '', contexts: [], requires: ['files'], enabled: {} },
      { id: 'reports', title: '报告', description: '', contexts: [], requires: ['pdf'], enabled: {} },
      { id: 'web_read', title: '网页', description: '', contexts: [], requires: [], enabled: {}, default_enabled: false },
    ],
  }
  const model = { provider: 'custom', model_name: 'model-a', base_url: 'https://example.test/v1', temperature: 0.2,
    max_steps: 30, context_budget: 32000, max_context_turns: 25, has_api_key: true, api_key_masked: '***' }
  const writes: { path: string; body: Record<string, unknown> }[] = []
  const acknowledged: string[] = []
  const options: SaveSettingsOptions = {
    model: { ...model }, savedModel: { ...model }, apiKey: '', catalog, draft: capabilityDraft(catalog),
    signal: new AbortController().signal,
    onPolicySaved: () => { acknowledged.push('policy') }, onModelSaved: () => { acknowledged.push('model') },
    fetcher: async (url, init) => {
      const body = JSON.parse(init!.body as string)
      writes.push({ path: String(url), body })
      return Response.json(String(url).endsWith('/capabilities') ? { ...catalog, ...body, version: 8 } : { success: true, config: body })
    },
  }
  return { catalog, model, writes, acknowledged, options }
}

test('unchanged settings do not write either persistence boundary', async () => {
  const { options, writes } = fixture()
  await saveSettings(options)
  assert.equal(writes.length, 0)
})

test('all draft validation completes before the first write, including hidden network fields', async () => {
  const { options, writes } = fixture()
  options.model.model_name = 'changed'
  options.draft = toggleCapability(options.catalog, options.draft, 'web_read', true)
  await assert.rejects(saveSettings(options), (error) => error instanceof SettingsValidationError && error.page === 'web' && error.field === 'web-domains')
  assert.equal(writes.length, 0)
})

test('numeric zero is a valid temperature and blank or out-of-range fields are rejected', async () => {
  const { options, writes } = fixture()
  options.model.temperature = 0
  await saveSettings(options)
  assert.equal(writes[0].body.temperature, 0)
  for (const invalid of [NaN, -1, 2.1]) {
    options.model.temperature = invalid
    await assert.rejects(saveSettings(options), SettingsValidationError)
  }
  options.model.temperature = 0
  options.model.model_name = '   '
  await assert.rejects(saveSettings(options), SettingsValidationError)
})

test('one save coordinates policy CAS first, then model; the secret is only sent to its endpoint', async () => {
  const { options, writes, acknowledged } = fixture()
  options.draft = toggleCapability(options.catalog, options.draft, 'files', false)
  options.apiKey = ' synthetic-new-credential '
  await saveSettings(options)
  assert.deepEqual(writes.map((item) => item.path), ['/api/agent/capabilities', '/api/agent/config'])
  assert.equal(writes[0].body.expected_version, 7)
  assert.equal(writes[0].body.catalog_version, 'catalog-v1')
  assert.deepEqual(writes[0].body.disabled_ids, ['files'])
  assert.equal(writes[0].body.api_key, undefined)
  assert.equal(writes[1].body.api_key, options.apiKey.trim())
  assert.equal(writes[1].body.has_api_key, undefined)
  assert.equal(writes[1].body.api_key_masked, undefined)
  assert.deepEqual(acknowledged, ['policy', 'model'])
})

test('blank secret is omitted to preserve the existing backend credential contract', async () => {
  const { options, writes } = fixture()
  options.model.model_name = 'changed'
  await saveSettings(options)
  assert.equal(Object.hasOwn(writes[0].body, 'api_key'), false)
  assert.equal(writes.length, 1)
})

test('a policy version conflict prevents any model write and keeps the draft', async () => {
  const { options, acknowledged } = fixture()
  options.model.model_name = 'changed'
  options.draft = toggleCapability(options.catalog, options.draft, 'files', false)
  let calls = 0
  options.fetcher = async () => { calls++; return Response.json({ detail: 'conflict' }, { status: 409 }) }
  await assert.rejects(saveSettings(options), (error) => error instanceof SettingsSaveError && error.conflict && !error.partial)
  assert.equal(calls, 1)
  assert.deepEqual(acknowledged, [])
  assert.deepEqual(options.draft.disabledIds, ['files'])
  assert.equal(options.model.model_name, 'changed')
})

test('partial success advances only the acknowledged baseline; retry sends only the model', async () => {
  const { options, writes, acknowledged } = fixture()
  options.model.model_name = 'changed'
  options.draft = toggleCapability(options.catalog, options.draft, 'files', false)
  const fetcher = options.fetcher!
  options.onPolicySaved = (catalog) => {
    options.catalog = catalog
    options.draft = capabilityDraft(catalog)
    acknowledged.push('policy')
  }
  options.fetcher = async (url, init) => String(url).endsWith('/config')
    ? Response.json({ detail: 'disk unavailable' }, { status: 503 }) : fetcher(url, init)
  await assert.rejects(saveSettings(options), (error) => error instanceof SettingsSaveError && error.partial && error.page === 'model' && error.message.includes('能力与网页访问已保存'))
  assert.deepEqual(acknowledged, ['policy'])
  assert.equal(policyChanged(options.catalog, options.draft), false)
  assert.equal(modelChanged(options.model, options.savedModel, ''), true)
  options.fetcher = fetcher
  await saveSettings(options)
  assert.deepEqual(writes.map((item) => item.path), ['/api/agent/capabilities', '/api/agent/config'])
  assert.deepEqual(acknowledged, ['policy', 'model'])
})

test('network failure never acknowledges a write or changes a policy version', async () => {
  const { options, acknowledged } = fixture()
  options.draft = toggleCapability(options.catalog, options.draft, 'files', false)
  options.fetcher = async () => { throw new TypeError('Failed to fetch') }
  await assert.rejects(saveSettings(options), SettingsSaveError)
  assert.equal(options.catalog.version, 7)
  assert.deepEqual(acknowledged, [])
})

test('aborted late success cannot update either state callback', async () => {
  const { options, acknowledged } = fixture()
  const controller = new AbortController()
  options.signal = controller.signal
  options.model.model_name = 'changed'
  options.fetcher = async () => { controller.abort(); return Response.json({ success: true, config: options.model }) }
  await saveSettings(options)
  assert.deepEqual(acknowledged, [])
})

test('failed model confirmation is not treated as a successful save', async () => {
  const { options, acknowledged } = fixture()
  options.model.model_name = 'changed'
  options.fetcher = async () => Response.json({ success: false })
  await assert.rejects(saveSettings(options), SettingsSaveError)
  assert.deepEqual(acknowledged, [])
})

test('dependencies are resolved transitively without granting other capabilities', () => {
  const { options } = fixture()
  options.draft = toggleCapability(options.catalog, options.draft, 'files', false)
  assert.equal(effectiveCapability(options.catalog, options.draft, 'reports'), false)
  assert.equal(effectiveCapability(options.catalog, options.draft, 'web_read'), false)
  assert.deepEqual(options.draft.enabledIds, [])
})

test('default-off grant is explicit; disabling removes the grant and keeps network limits', () => {
  const { options } = fixture()
  options.draft.domainText = 'pages.example.test'
  options.draft = toggleCapability(options.catalog, options.draft, 'web_read', true)
  assert.deepEqual(options.draft.enabledIds, ['web_read'])
  options.draft = toggleCapability(options.catalog, options.draft, 'web_read', false)
  assert.deepEqual(options.draft.enabledIds, [])
  assert.deepEqual(options.draft.disabledIds, ['web_read'])
  assert.equal(options.draft.domainText, 'pages.example.test')
  assert.equal(options.catalog.web_read!.allowed_domains.length, 0)
})

test('API validation arrays and non-JSON failures produce readable errors', async () => {
  await assert.rejects(settingsRequest('/x', {}, async () => Response.json({ detail: [{ msg: 'invalid integer' }] }, { status: 422 })), /invalid integer/)
  await assert.rejects(settingsRequest('/x', {}, async () => new Response('bad gateway', { status: 502 })), /HTTP 502/)
})

test('known model length and domain-count limits are checked before any write', async () => {
  const { options, writes } = fixture()
  options.model.model_name = 'x'.repeat(257)
  await assert.rejects(saveSettings(options), SettingsValidationError)
  options.model.model_name = 'changed'
  options.draft.domainText = Array.from({ length: 65 }, (_, index) => `host${index}.example.test`).join('\n')
  await assert.rejects(saveSettings(options), SettingsValidationError)
  options.draft.domainText = ''
  options.apiKey = 'x'.repeat(4097)
  await assert.rejects(saveSettings(options), SettingsValidationError)
  assert.deepEqual(writes, [])
})

test('a hung request times out and explicitly reports uncertain server state', async (context) => {
  context.mock.timers.enable({ apis: ['setTimeout'] })
  const result = settingsRequest('/x', {}, async (_url, init) => new Promise<Response>((_resolve, reject) => {
    init!.signal!.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')), { once: true })
  }))
  const assertion = assert.rejects(result, /请求超时，请重新载入核对服务器上的保存结果/)
  context.mock.timers.tick(30000)
  await assertion
})
