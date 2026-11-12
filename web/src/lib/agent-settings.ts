import type { LLMConfigData } from '../components/panels/agent/drawer/types.ts'

export type SettingsPage = 'model' | 'capabilities' | 'web'
export type Capability = {
  id: string
  title: string
  description: string
  contexts: string[]
  requires: string[]
  enabled: Record<string, boolean>
  default_enabled?: boolean
}
export type WebReadPolicy = {
  allowed_domains: string[]
  allowed_ports: number[]
  connect_timeout_seconds: number
  total_timeout_seconds: number
  max_response_bytes: number
  max_text_chars: number
  max_concurrent_requests: number
  requests_per_minute: number
}
export type CapabilityCatalog = {
  version: number
  catalog_version: string
  disabled_ids: string[]
  enabled_ids?: string[]
  web_read?: WebReadPolicy
  capabilities: Capability[]
}
export type CapabilityDraft = {
  disabledIds: string[]
  enabledIds: string[]
  webPolicy: WebReadPolicy
  domainText: string
}

export const defaultWebPolicy: WebReadPolicy = {
  allowed_domains: [], allowed_ports: [443], connect_timeout_seconds: 3, total_timeout_seconds: 10,
  max_response_bytes: 524288, max_text_chars: 40000, max_concurrent_requests: 2, requests_per_minute: 20,
}
export const webLimits = [
  ['connect_timeout_seconds', '连接超时（秒）', 1, 10],
  ['total_timeout_seconds', '整体超时（秒）', 2, 30],
  ['max_response_bytes', '响应字节上限', 1024, 2097152],
  ['max_text_chars', '返回文本字符上限', 100, 100000],
  ['max_concurrent_requests', '当前认证范围并发上限', 1, 8],
  ['requests_per_minute', '每分钟请求上限', 1, 120],
] as const

export function capabilityDraft(catalog: CapabilityCatalog): CapabilityDraft {
  const policy = catalog.web_read || defaultWebPolicy
  return {
    disabledIds: [...catalog.disabled_ids], enabledIds: [...(catalog.enabled_ids || [])],
    webPolicy: { ...policy, allowed_domains: [...policy.allowed_domains], allowed_ports: [...policy.allowed_ports] },
    domainText: policy.allowed_domains.join('\n'),
  }
}

export function selectedCapability(catalog: CapabilityCatalog, draft: CapabilityDraft, id: string): boolean {
  const item = catalog.capabilities.find((entry) => entry.id === id)
  return !!item && !draft.disabledIds.includes(id) && ((item.default_enabled ?? true) || draft.enabledIds.includes(id))
}

export function effectiveCapability(catalog: CapabilityCatalog, draft: CapabilityDraft, id: string, chain: string[] = []): boolean {
  if (chain.includes(id) || !selectedCapability(catalog, draft, id)) return false
  return catalog.capabilities.find((item) => item.id === id)!.requires.every((dependency) =>
    effectiveCapability(catalog, draft, dependency, [...chain, id]))
}

export function toggleCapability(catalog: CapabilityCatalog, draft: CapabilityDraft, id: string, checked: boolean): CapabilityDraft {
  const item = catalog.capabilities.find((entry) => entry.id === id)
  return {
    ...draft,
    disabledIds: [...draft.disabledIds.filter((value) => value !== id), ...(checked ? [] : [id])],
    enabledIds: [...draft.enabledIds.filter((value) => value !== id), ...(checked && item?.default_enabled === false ? [id] : [])],
  }
}

export function draftWebPolicy(draft: CapabilityDraft): WebReadPolicy {
  return { ...draft.webPolicy, allowed_domains: [...new Set(draft.domainText.split('\n').map((domain) => domain.trim()).filter(Boolean))] }
}

// Compare editable fields only: masked credentials and API response metadata are not drafts.
export function modelValues(config: LLMConfigData) {
  return {
    provider: config.provider, model_name: config.model_name.trim(), base_url: config.base_url.trim(),
    temperature: config.temperature, max_steps: config.max_steps ?? 30,
    context_budget: config.context_budget ?? 32000, max_context_turns: config.max_context_turns ?? 25,
  }
}

export function modelChanged(config: LLMConfigData, saved: LLMConfigData, key: string): boolean {
  return !!key.trim() || JSON.stringify(modelValues(config)) !== JSON.stringify(modelValues(saved))
}

export function policyChanged(catalog: CapabilityCatalog, draft: CapabilityDraft): boolean {
  const ids = (values: string[]) => JSON.stringify([...values].sort())
  return ids(draft.disabledIds) !== ids(catalog.disabled_ids) ||
    ids(draft.enabledIds) !== ids(catalog.enabled_ids || []) ||
    JSON.stringify(draftWebPolicy(draft)) !== JSON.stringify(catalog.web_read || defaultWebPolicy)
}

export class SettingsValidationError extends Error {
  page: SettingsPage
  field: string
  constructor(message: string, page: SettingsPage, field: string) {
    super(message)
    this.page = page
    this.field = field
  }
}

export function validateSettings(model: LLMConfigData, catalog: CapabilityCatalog, draft: CapabilityDraft, apiKey = '') {
  if (!model.model_name.trim()) throw new SettingsValidationError('请输入模型标识', 'model', 'model-name')
  for (const [value, limit, field, label] of [
    [model.provider, 64, 'model-provider', '模型提供商'], [model.model_name, 256, 'model-name', '模型标识'],
    [model.base_url, 2048, 'model-url', 'Base URL'], [apiKey, 4096, 'model-key', 'API Key'],
  ] as const) {
    if (value.length > limit) throw new SettingsValidationError(`${label}不能超过 ${limit} 个字符`, 'model', field)
  }
  const numericLimits = [
    ['temperature', '模型温度', 0, 2, false], ['max_steps', '执行轮数上限', 5, 200, true],
    ['context_budget', '上下文字符预算', 4000, 2000000, true], ['max_context_turns', '保留对话轮数', 5, 200, true],
  ] as const
  const values = modelValues(model)
  for (const [key, label, min, max, integer] of numericLimits) {
    const value = values[key]
    if (!Number.isFinite(value) || (integer && !Number.isInteger(value)) || value < min || value > max) {
      throw new SettingsValidationError(`${label}必须为 ${min} 至 ${max} 的${integer ? '整数' : '数值'}`, 'model', `model-${key}`)
    }
  }
  const policy = draftWebPolicy(draft)
  if (policy.allowed_domains.length > 64) {
    throw new SettingsValidationError('最多允许 64 个精确域名', 'web', 'web-domains')
  }
  for (const [key, label, min, max] of webLimits) {
    if (!Number.isInteger(policy[key]) || policy[key] < min || policy[key] > max) {
      throw new SettingsValidationError(`${label}必须为 ${min} 至 ${max} 的整数`, 'web', `web-${key}`)
    }
  }
  if (policy.connect_timeout_seconds > policy.total_timeout_seconds) {
    throw new SettingsValidationError('连接超时不能大于整体超时', 'web', 'web-connect_timeout_seconds')
  }
  if (selectedCapability(catalog, draft, 'web_read') && !policy.allowed_domains.length) {
    throw new SettingsValidationError('启用网页读取前必须批准至少一个完整域名', 'web', 'web-domains')
  }
}

export class SettingsRequestError extends Error {
  status: number
  constructor(message: string, status: number) {
    super(message)
    this.status = status
  }
}

export async function settingsRequest<T>(url: string, options: RequestInit = {}, fetcher: typeof fetch = fetch): Promise<T> {
  const controller = new AbortController()
  const abort = () => controller.abort()
  if (options.signal?.aborted) controller.abort()
  options.signal?.addEventListener('abort', abort, { once: true })
  const timer = setTimeout(abort, 30000)
  try {
    const response = await fetcher(url, { ...options, signal: controller.signal })
    const data = await response.json().catch(() => null)
    if (!response.ok || !data) {
      const detail = data?.detail
      const message = typeof detail === 'string' ? detail : Array.isArray(detail)
        ? detail.map((entry) => entry?.msg || '参数无效').join('；') : `请求失败（HTTP ${response.status}）`
      throw new SettingsRequestError(message, response.status)
    }
    return data as T
  } catch (cause) {
    if (controller.signal.aborted && !options.signal?.aborted) {
      throw new Error('请求超时，请重新载入核对服务器上的保存结果')
    }
    throw cause
  } finally {
    clearTimeout(timer)
    options.signal?.removeEventListener('abort', abort)
  }
}

export type SaveSettingsOptions = {
  model: LLMConfigData
  savedModel: LLMConfigData
  apiKey: string
  catalog: CapabilityCatalog
  draft: CapabilityDraft
  signal: AbortSignal
  onPolicySaved: (catalog: CapabilityCatalog) => void
  onModelSaved: (model: LLMConfigData) => void
  fetcher?: typeof fetch
}

export class SettingsSaveError extends Error {
  page: SettingsPage
  partial: boolean
  conflict: boolean
  constructor(cause: unknown, page: SettingsPage, partial: boolean) {
    const detail = cause instanceof Error ? cause.message : '网络异常'
    super(`${partial ? '能力与网页访问已保存；' : ''}${page === 'model' ? '模型配置' : '能力与网页访问'}保存失败：${detail}。未完成的更改已保留。`)
    this.page = page
    this.partial = partial
    this.conflict = cause instanceof SettingsRequestError && cause.status === 409
  }
}

// The credential file and scoped SQLite policy have separate persistence boundaries.
// Validate everything first, check policy CAS first, and acknowledge each confirmed write.
// A later model failure must never erase the remaining draft or pretend to roll back SQLite.
export async function saveSettings(options: SaveSettingsOptions) {
  const { model, savedModel, apiKey, catalog, draft, signal, onPolicySaved, onModelSaved, fetcher } = options
  validateSettings(model, catalog, draft, apiKey)
  let policySaved = false
  let page: SettingsPage = 'capabilities'
  try {
    if (policyChanged(catalog, draft)) {
      const next = await settingsRequest<CapabilityCatalog>('/api/agent/capabilities', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' }, signal,
        body: JSON.stringify({ expected_version: catalog.version, catalog_version: catalog.catalog_version,
          disabled_ids: draft.disabledIds, enabled_ids: draft.enabledIds, web_read: draftWebPolicy(draft) }),
      }, fetcher)
      if (signal.aborted) return
      onPolicySaved(next)
      policySaved = true
    }
    page = 'model'
    if (modelChanged(model, savedModel, apiKey)) {
      const next = await settingsRequest<{ success: boolean; config: LLMConfigData }>('/api/agent/config', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, signal,
        body: JSON.stringify({ ...modelValues(model), api_key: apiKey.trim() || undefined }),
      }, fetcher)
      if (signal.aborted) return
      if (!next.success || !next.config) throw new Error('服务端未确认保存结果')
      onModelSaved(next.config)
    }
  } catch (cause) {
    if (!signal.aborted) throw new SettingsSaveError(cause, page, policySaved)
  }
}
