import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { Loader2, X } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { getAccessToken } from '@/lib/auth'
import {
  capabilityDraft, modelChanged, policyChanged, saveSettings, settingsRequest,
  SettingsSaveError, SettingsValidationError,
} from '@/lib/agent-settings'
import type { CapabilityCatalog, CapabilityDraft, SettingsPage } from '@/lib/agent-settings'
import type { LLMConfigData } from './drawer/types'
import { CapabilitySettings } from './CapabilitySettings'
import { ModelCombobox } from './ModelCombobox'

const inputClass = 'w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 disabled:opacity-60 dark:border-slate-700 dark:bg-slate-950 dark:text-slate-100'
const providers = [
  { id: 'deepseek', label: 'DeepSeek', model: 'deepseek-chat', url: 'https://api.deepseek.com' },
  { id: 'qwen', label: '通义千问 / DashScope', model: 'qwen-plus', url: 'https://dashscope.aliyuncs.com/compatible-mode/v1' },
  { id: 'ollama', label: 'Ollama', model: 'qwen2.5-coder:7b', url: 'http://localhost:11434/v1' },
  { id: 'openai', label: 'OpenAI', model: 'gpt-4o', url: 'https://api.openai.com/v1' },
  { id: 'custom', label: '自定义 OpenAI 兼容接口', model: '', url: '' },
]
const modelLimits = [
  ['max_steps', '执行轮数上限', 5, 200, 1], ['temperature', '模型温度', 0, 2, 0.1],
  ['context_budget', '上下文字符预算', 4000, 2000000, 1000], ['max_context_turns', '保留对话轮数', 5, 200, 1],
] as const
const pages: { id: SettingsPage; label: string }[] = [
  { id: 'model', label: '模型连接' }, { id: 'capabilities', label: '能力权限' }, { id: 'web', label: '网页访问' },
]

type Props = { onClose: () => void; onModelSaved: (model: LLMConfigData) => void; returnFocusTo: HTMLElement | null }

export function AgentSettingsDialog({ onClose, onModelSaved, returnFocusTo }: Props) {
  const dialogRef = useRef<HTMLDialogElement>(null)
  const titleRef = useRef<HTMLHeadingElement>(null)
  const contentRef = useRef<HTMLDivElement>(null)
  const confirmCancelRef = useRef<HTMLButtonElement>(null)
  const returnFocusRef = useRef<HTMLElement | null>(null)
  const requests = useRef(new Set<AbortController>())
  const modelsRequest = useRef<AbortController | null>(null)
  const savingRef = useRef(false)
  const identityAtOpen = useRef(getAccessToken())
  const [page, setPage] = useState<SettingsPage>('model')
  const [savedModel, setSavedModel] = useState<LLMConfigData | null>(null)
  const [model, setModel] = useState<LLMConfigData | null>(null)
  const [apiKey, setApiKey] = useState('')
  const [catalog, setCatalog] = useState<CapabilityCatalog | null>(null)
  const [draft, setDraft] = useState<CapabilityDraft | null>(null)
  const [loading, setLoading] = useState(true)
  const [reload, setReload] = useState(0)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [errorField, setErrorField] = useState('')
  const [conflict, setConflict] = useState(false)
  const [notice, setNotice] = useState('')
  const [confirmAction, setConfirmAction] = useState<'close' | 'reload' | null>(null)
  const [availableModels, setAvailableModels] = useState<string[]>([])
  const [fetchingModels, setFetchingModels] = useState(false)
  const [modelNotice, setModelNotice] = useState('')
  const [testing, setTesting] = useState(false)
  const [testResult, setTestResult] = useState<{ ok: boolean; message: string } | null>(null)
  const modelDirty = !!model && !!savedModel && modelChanged(model, savedModel, apiKey)
  const policyDirty = !!catalog && !!draft && policyChanged(catalog, draft)
  const dirty = modelDirty || policyDirty

  useEffect(() => {
    const dialog = dialogRef.current!
    const opener = returnFocusTo || document.activeElement as HTMLElement
    dialog.showModal()
    titleRef.current?.focus()
    return () => {
      requests.current.forEach((controller) => controller.abort())
      dialog.close()
      if (opener?.isConnected) opener.focus({ preventScroll: true })
    }
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    requests.current.add(controller)
    setLoading(true)
    setError('')
    setNotice('')
    setTestResult(null)
    setConflict(false)
    setModel(null)
    setSavedModel(null)
    setCatalog(null)
    setDraft(null)
    setApiKey('')
    setErrorField('')
    setAvailableModels([])
    setModelNotice('')
    Promise.all([
      settingsRequest<{ config: LLMConfigData }>('/api/agent/config', { signal: controller.signal }),
      settingsRequest<CapabilityCatalog>('/api/agent/capabilities', { signal: controller.signal }),
    ]).then(([config, nextCatalog]) => {
      if (controller.signal.aborted) return
      setModel(config.config)
      setSavedModel(config.config)
      setApiKey('')
      setCatalog(nextCatalog)
      setDraft(capabilityDraft(nextCatalog))
    }).catch((cause) => {
      if (!controller.signal.aborted) setError(`加载配置失败：${cause instanceof Error ? cause.message : '网络异常'}`)
    }).finally(() => {
      if (!controller.signal.aborted) setLoading(false)
      requests.current.delete(controller)
    })
    return () => controller.abort()
  }, [reload])

  useEffect(() => {
    if (confirmAction) confirmCancelRef.current?.focus()
    else returnFocusRef.current?.focus()
  }, [confirmAction])

  useEffect(() => { contentRef.current?.scrollTo({ top: 0 }) }, [page])

  useEffect(() => {
    if (!dirty && !saving) return
    const warn = (event: BeforeUnloadEvent) => {
      // Identity changes must be able to clear the old user's workspace without
      // a dirty-form prompt cancelling the auth interceptor's forced reload.
      if (getAccessToken() !== identityAtOpen.current) return
      event.preventDefault()
      event.returnValue = ''
    }
    window.addEventListener('beforeunload', warn)
    return () => window.removeEventListener('beforeunload', warn)
  }, [dirty, saving])

  function changed() { setNotice(''); if (!conflict) setError(''); setErrorField('') }
  function changeModel(next: LLMConfigData) {
    changed()
    modelsRequest.current?.abort()
    setFetchingModels(false)
    setAvailableModels([])
    setModelNotice('')
    setTestResult(null)
    setModel(next)
  }
  function reloadSettings() {
    if (savingRef.current) return
    requests.current.forEach((controller) => controller.abort())
    modelsRequest.current = null
    setFetchingModels(false)
    setTesting(false)
    setReload((value) => value + 1)
  }
  function requestAction(action: 'close' | 'reload') {
    if (savingRef.current) return
    if (dirty) {
      returnFocusRef.current = document.activeElement as HTMLElement
      setConfirmAction(action)
    } else if (action === 'close') onClose()
    else reloadSettings()
  }

  async function save() {
    if (!model || !savedModel || !catalog || !draft || savingRef.current || !dirty) return
    savingRef.current = true
    setSaving(true)
    setError(''); setNotice(''); setErrorField('')
    const controller = new AbortController()
    requests.current.add(controller)
    try {
      await saveSettings({ model, savedModel, apiKey, catalog, draft, signal: controller.signal,
        onPolicySaved(next) { setCatalog(next); setDraft(capabilityDraft(next)) },
        onModelSaved(next) { setModel(next); setSavedModel(next); setApiKey(''); onModelSaved(next) },
      })
      if (!controller.signal.aborted) setNotice(policyDirty ? '配置已保存。能力与网页访问策略对新任务生效，运行中的任务继续使用原版本。' : '配置已保存。')
    } catch (cause) {
      if (controller.signal.aborted) return
      setError(cause instanceof Error ? cause.message : '保存失败，未完成的更改已保留')
      if (cause instanceof SettingsValidationError) {
        setPage(cause.page)
        setErrorField(cause.field)
        requestAnimationFrame(() => {
          const field = document.getElementById(cause.field)
          const details = field?.closest('details')
          if (details) details.open = true
          field?.focus()
        })
      } else if (cause instanceof SettingsSaveError) {
        setPage(cause.page)
        setConflict(cause.conflict)
      }
    } finally {
      savingRef.current = false
      if (!controller.signal.aborted) setSaving(false)
      requests.current.delete(controller)
    }
  }

  async function fetchModels() {
    if (!model || fetchingModels) return
    const controller = new AbortController()
    modelsRequest.current = controller
    requests.current.add(controller)
    setFetchingModels(true); setModelNotice('')
    try {
      const data = await settingsRequest<{ models?: string[]; source?: string; message?: string }>('/api/agent/models', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, signal: controller.signal,
        body: JSON.stringify({ provider: model.provider, base_url: model.base_url, api_key: apiKey.trim() || undefined }),
      })
      if (!controller.signal.aborted) {
        const models = [...new Set((data.models || []).filter((name) => typeof name === 'string' && name.trim()))]
        setAvailableModels(models)
        setModelNotice(models.length === 0 ? '未获取到可用模型，可重试或手动输入模型标识。' : data.source === 'remote' ? '' : data.message || '已载入常用预设，也可手动输入模型标识')
      }
    } catch (cause) {
      if (!controller.signal.aborted) setModelNotice(`获取模型失败：${cause instanceof Error ? cause.message : '网络异常'}`)
    } finally {
      if (!controller.signal.aborted) setFetchingModels(false)
      requests.current.delete(controller)
    }
  }

  async function testConnection() {
    if (testing || modelDirty) return
    const controller = new AbortController()
    requests.current.add(controller)
    setTesting(true); setTestResult(null)
    try {
      const data = await settingsRequest<{ success: boolean; message?: string; error?: string }>('/api/agent/config/test', { method: 'POST', signal: controller.signal })
      if (!controller.signal.aborted) setTestResult({ ok: data.success, message: data.message || data.error || '测试完成' })
    } catch (cause) {
      if (!controller.signal.aborted) setTestResult({ ok: false, message: cause instanceof Error ? cause.message : '测试连接失败' })
    } finally {
      if (!controller.signal.aborted) setTesting(false)
      requests.current.delete(controller)
    }
  }

  return createPortal(
    <dialog ref={dialogRef} aria-labelledby={confirmAction ? 'settings-confirm-title' : 'agent-settings-title'}
      onCancel={(event) => { event.preventDefault(); if (confirmAction) setConfirmAction(null); else requestAction('close') }}
      className="m-auto h-dvh max-h-dvh w-screen max-w-none overflow-hidden border border-slate-200 bg-white p-0 text-slate-900 backdrop:bg-slate-950/50 dark:border-slate-800 dark:bg-slate-900 dark:text-slate-100 sm:h-[min(780px,92dvh)] sm:max-h-[92dvh] sm:w-[min(960px,calc(100vw-3rem))] sm:rounded-lg">
      {confirmAction && <div className="flex h-full items-center justify-center p-6">
        <div className="max-w-sm space-y-5">
          <h2 id="settings-confirm-title" className="text-lg font-semibold">{confirmAction === 'close' ? '放弃未保存的更改？' : '重新载入并放弃更改？'}</h2>
          <p className="text-sm leading-6 text-slate-500 dark:text-slate-400">{confirmAction === 'close' ? '关闭后，本次未保存的模型、能力和网页访问修改将被丢弃。' : '将读取服务器上的最新配置，并丢弃当前未保存的修改。已经保存的部分不会回退。'}</p>
          <div className="flex justify-end gap-3">
            <Button ref={confirmCancelRef} variant="outline" onClick={() => setConfirmAction(null)}>继续编辑</Button>
            <Button variant="destructive" onClick={() => {
              const action = confirmAction
              setConfirmAction(null)
              if (action === 'close') onClose()
              else reloadSettings()
            }}>{confirmAction === 'close' ? '放弃并关闭' : '放弃并重新载入'}</Button>
          </div>
        </div>
      </div>}
      <form hidden={!!confirmAction} noValidate onSubmit={(event) => { event.preventDefault(); void save() }} className={confirmAction ? 'hidden' : 'flex h-full flex-col'}>
        <header className="flex shrink-0 items-start justify-between gap-4 border-b border-slate-200 px-5 py-4 dark:border-slate-800 sm:px-6">
          <div><h2 ref={titleRef} tabIndex={-1} id="agent-settings-title" className="text-lg font-semibold outline-none">模型与能力配置</h2><p className="mt-1 text-sm text-slate-500 dark:text-slate-400">管理员设置 · 所有分区统一保存</p></div>
          <button type="button" aria-label="关闭配置" disabled={saving} onClick={() => requestAction('close')} className="rounded-md p-2 text-slate-500 hover:bg-slate-100 focus-visible:outline-2 focus-visible:outline-blue-500 disabled:opacity-40 dark:hover:bg-slate-800"><X className="h-5 w-5" /></button>
        </header>
        <div className="flex min-h-0 flex-1 flex-col sm:flex-row">
          <nav aria-label="配置分类" className="shrink-0 border-b border-slate-200 px-3 dark:border-slate-800 sm:w-44 sm:border-b-0 sm:border-r sm:px-3 sm:py-5">
            <div role="tablist" aria-label="配置分区" className="flex sm:flex-col" onKeyDown={(event) => {
              const offset = ['ArrowRight', 'ArrowDown'].includes(event.key) ? 1 : ['ArrowLeft', 'ArrowUp'].includes(event.key) ? -1 : 0
              if (!offset && event.key !== 'Home' && event.key !== 'End') return
              event.preventDefault()
              const index = pages.findIndex((item) => item.id === page)
              const next = event.key === 'Home' ? 0 : event.key === 'End' ? pages.length - 1 : (index + offset + pages.length) % pages.length
              setPage(pages[next].id)
              document.getElementById(`settings-tab-${pages[next].id}`)?.focus()
            }}>
              {pages.map((item) => <button key={item.id} type="button" role="tab" id={`settings-tab-${item.id}`} aria-selected={page === item.id}
                aria-controls={`settings-panel-${item.id}`} tabIndex={page === item.id ? 0 : -1} onClick={() => setPage(item.id)}
                className={`flex-1 whitespace-nowrap border-b-2 px-3 py-3 text-sm font-medium focus-visible:outline-2 focus-visible:outline-blue-500 sm:flex-none sm:rounded-md sm:border-b-0 sm:text-left ${page === item.id ? 'border-blue-600 text-blue-700 sm:bg-slate-100 dark:text-blue-400 dark:sm:bg-slate-800' : 'border-transparent text-slate-600 hover:bg-slate-50 dark:text-slate-400 dark:hover:bg-slate-800'}`}>{item.label}</button>)}
            </div>
          </nav>
          <div ref={contentRef} data-settings-content aria-busy={loading} className="min-h-0 min-w-0 flex-1 overflow-y-auto p-5 sm:p-6">
            {loading ? <p role="status" className="flex items-center gap-2 py-6 text-sm text-slate-500 dark:text-slate-400"><Loader2 className="h-4 w-4 animate-spin" />正在加载配置……</p> : model && catalog && draft ? <>
              <section id="settings-panel-model" role="tabpanel" aria-labelledby="settings-tab-model" hidden={page !== 'model'}>
                <h3 className="text-base font-semibold">模型连接</h3>
                <p className="mt-1 text-sm leading-6 text-slate-500 dark:text-slate-400">服务级默认模型，供未设置专属模型的 BOT 使用。</p>
                <fieldset disabled={saving || testing} className="mt-5 space-y-5">
                  <div><label htmlFor="model-provider" className="mb-2 block text-sm font-medium">模型提供商</label>
                      <select id="model-provider" className={inputClass} value={model.provider} onChange={(event) => {
                        const provider = providers.find((item) => item.id === event.target.value)!
                        setApiKey('')
                        changeModel({ ...model, provider: provider.id, model_name: provider.model, base_url: provider.url })
                      }}>{providers.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}</select>
                  </div>
                  <div><label htmlFor="model-name" className="mb-2 block text-sm font-medium">模型标识</label>
                    <ModelCombobox value={model.model_name} models={availableModels} disabled={saving || testing}
                      invalid={errorField === 'model-name'} inputClassName={inputClass} fetching={fetchingModels} onFetch={fetchModels}
                      onChange={(name) => { changed(); setTestResult(null); setModel({ ...model, model_name: name }) }} />
                    <span id="model-choice-hint" className="sr-only">可从列表选择，也可直接输入模型标识。</span>
                    {modelNotice && <p role="status" className="mt-2 text-sm leading-6 text-slate-500 dark:text-slate-400">{modelNotice}</p>}
                  </div>
                  <div><label htmlFor="model-url" className="mb-2 block text-sm font-medium">Base URL</label><input id="model-url" value={model.base_url} maxLength={2048} onChange={(event) => changeModel({ ...model, base_url: event.target.value })} placeholder="https://api.deepseek.com" spellCheck={false} className={`${inputClass} font-mono`} /></div>
                  <div><label htmlFor="model-key" className="mb-2 block text-sm font-medium">API Key</label>
                    <input id="model-key" type="password" autoComplete="new-password" maxLength={4096} value={apiKey} onChange={(event) => { changeModel(model); setApiKey(event.target.value) }} placeholder={savedModel?.has_api_key && model.provider === savedModel.provider ? '留空保留已保存的密钥' : '输入密钥（Ollama 通常无需填写）'} className={`${inputClass} font-mono`} />
                    <p className="mt-2 text-sm leading-6 text-slate-500 dark:text-slate-400">{savedModel?.has_api_key && model.provider === savedModel.provider ? `已配置 ${savedModel.api_key_masked || '密钥'}。同一提供商下留空会沿用已有密钥，包括更改 Base URL 后。` : '切换提供商不会沿用原提供商的密钥。'}</p>
                  </div>
                  <details className="border-t border-slate-200 pt-4 dark:border-slate-800">
                    <summary className="cursor-pointer rounded text-sm font-medium focus-visible:outline-2 focus-visible:outline-blue-500">高级参数</summary>
                    <div className="mt-4 grid grid-cols-1 gap-4 sm:grid-cols-2">{modelLimits.map(([key, label, min, max, step]) => <div key={key}>
                      <label htmlFor={`model-${key}`} className="mb-2 block text-sm">{label}</label>
                      <input id={`model-${key}`} type="number" value={Number.isFinite(model[key]) ? model[key] : ''} min={min} max={max} step={step} aria-invalid={errorField === `model-${key}`} onChange={(event) => { changed(); setModel({ ...model, [key]: event.target.valueAsNumber }) }} className={inputClass} />
                    </div>)}</div>
                    <p className="mt-4 text-sm leading-6 text-slate-500 dark:text-slate-400">上下文按字符数控制，超出预算时压缩较早内容；执行轮数不等同于工具调用次数。</p>
                  </details>
                </fieldset>
                <div className="mt-5 border-t border-slate-200 pt-4 dark:border-slate-800">
                  <div data-model-test-row className="flex flex-wrap items-center gap-x-3 gap-y-2">
                    <Button type="button" variant="outline" title="向服务器已保存的模型连接发送 Ping 请求" onClick={testConnection} disabled={testing || saving || modelDirty}>{testing && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}{testing ? '测试中……' : '测试已保存连接'}</Button>
                    {testResult && <p data-model-test-result role={testResult.ok ? 'status' : 'alert'} className={`min-w-0 max-w-full text-sm break-words ${testResult.ok ? 'text-emerald-700 dark:text-emerald-400' : 'text-red-600 dark:text-red-400'}`}>{testResult.message}</p>}
                  </div>
                  {modelDirty && <p className="mt-2 text-sm text-slate-500 dark:text-slate-400">先保存模型修改，再测试实际生效的连接。</p>}
                </div>
              </section>
              <CapabilitySettings catalog={catalog} draft={draft} page={page} onPageChange={setPage} disabled={saving}
                errorField={errorField} onChange={(next) => { changed(); setDraft(next) }} />
            </> : null}
          </div>
        </div>
        <footer className="shrink-0 border-t border-slate-200 px-5 py-4 dark:border-slate-800 sm:px-6">
          {error && <div className="mb-3 flex flex-wrap items-start justify-between gap-3">
            <div role="alert" className="min-w-0 flex-1 basis-48 max-h-24 overflow-y-auto text-sm leading-6 text-red-600 dark:text-red-400">{error}{conflict && <span className="block">其他管理员或服务版本已更新配置，请重新载入后再编辑。</span>}</div>
            {!errorField && <Button type="button" variant="outline" disabled={loading || saving} onClick={() => requestAction('reload')}>重新载入</Button>}
          </div>}
          {notice && <p role="status" className="mb-3 text-sm leading-6 text-emerald-700 dark:text-emerald-400">{notice}</p>}
          <div className="flex flex-wrap items-center justify-between gap-3">
            <span aria-live="polite" className="text-sm text-slate-500 dark:text-slate-400">{saving ? '正在保存，请稍候……' : dirty ? '有未保存的更改' : ''}</span>
            <div className="ml-auto flex items-center gap-2">
              <Button type="button" variant="outline" disabled={saving} onClick={() => requestAction('close')}>关闭</Button>
              <Button type="submit" disabled={loading || saving || testing || !model || !catalog || !dirty || conflict}>{saving && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}{saving ? '保存中……' : '保存更改'}</Button>
            </div>
          </div>
        </footer>
      </form>
    </dialog>, document.body,
  )
}
