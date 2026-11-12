import type { CapabilityCatalog, CapabilityDraft, SettingsPage } from '@/lib/agent-settings'
import { effectiveCapability, selectedCapability, toggleCapability, webLimits } from '@/lib/agent-settings'

const contextLabels: Record<string, string> = { agent: '主会话', delegate: '协同任务', skill: '技能' }
const inputClass = 'w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 dark:border-slate-700 dark:bg-slate-950 dark:text-slate-100'
const groups = [
  { title: '数据与内容', ids: ['files', 'pdf', 'charts', 'knowledge', 'memory', 'prompt'] },
  { title: '执行与协作', ids: ['cloud', 'live_urls', 'skills', 'delegation', 'bot_creation'] },
]

type Props = {
  catalog: CapabilityCatalog
  draft: CapabilityDraft
  onChange: (draft: CapabilityDraft) => void
  page: SettingsPage
  onPageChange: (page: SettingsPage) => void
  disabled: boolean
  errorField: string
}

// All draft state and persistence belong to the settings dialog. Switching sections
// never saves, reloads, or discards edits.
export function CapabilitySettings({ catalog, draft, onChange, page, onPageChange, disabled, errorField }: Props) {
  const selected = (id: string) => selectedCapability(catalog, draft, id)
  const webAvailable = catalog.capabilities.some((item) => item.id === 'web_read')
  const known = groups.flatMap((group) => group.ids)
  const sections = [...groups, { title: '其他能力', ids: catalog.capabilities.filter((item) => !known.includes(item.id) && item.id !== 'web_read').map((item) => item.id) }]
  return (
    <>
      <section id="settings-panel-capabilities" role="tabpanel" aria-labelledby="settings-tab-capabilities" hidden={page !== 'capabilities'}>
        <h3 className="text-base font-semibold">能力权限</h3>
        <p className="mt-1 text-sm leading-6 text-slate-500 dark:text-slate-400">作用于当前认证范围内的所有 BOT。停用依赖项会使相关能力不可用，已有用户权限和审批规则仍然有效。</p>
        <fieldset disabled={disabled} aria-label="智能体能力管理" className="mt-5 space-y-6">
          {sections.map((group) => {
            const items = catalog.capabilities.filter((item) => group.ids.includes(item.id))
            if (!items.length) return null
            return <div key={group.title}>
              <h4 className="border-b border-slate-200 pb-2 text-sm font-medium dark:border-slate-800">{group.title}</h4>
              <div className="divide-y divide-slate-100 dark:divide-slate-800">
                {items.map((item) => {
                  const missing = item.requires.filter((id) => !effectiveCapability(catalog, draft, id))
                  return <label key={item.id} className="flex cursor-pointer items-start gap-3 py-4">
                    <input type="checkbox" aria-label={item.title} checked={selected(item.id)}
                      onChange={(event) => onChange(toggleCapability(catalog, draft, item.id, event.target.checked))}
                      className="mt-1 h-4 w-4 shrink-0 accent-blue-600 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500" />
                    <span className="min-w-0 flex-1">
                      <span className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
                        <span className="text-sm font-medium">{item.title}</span>
                        <span className="text-xs text-slate-500 dark:text-slate-400">{item.contexts.map((ctx) => contextLabels[ctx] || ctx).join(' / ')}</span>
                      </span>
                      <span className="mt-1 block text-sm leading-6 text-slate-500 dark:text-slate-400">{item.description}</span>
                      {selected(item.id) && missing.length > 0 && <span className="mt-1 block text-sm text-amber-700 dark:text-amber-400">依赖未启用：{missing.map((id) => catalog.capabilities.find((entry) => entry.id === id)?.title || id).join('、')}。此能力当前不可用。</span>}
                    </span>
                  </label>
                })}
              </div>
            </div>
          })}
        </fieldset>
        {webAvailable && <div className="mt-5 border-t border-slate-200 pt-4 dark:border-slate-800">
          <button type="button" onClick={() => onPageChange('web')} className="rounded text-sm font-medium text-blue-600 underline-offset-4 hover:underline focus-visible:outline-2 focus-visible:outline-blue-500 dark:text-blue-400">配置网页访问 →</button>
          <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">网页读取默认关闭，需单独批准允许访问的域名。</p>
        </div>}
      </section>
      {webAvailable && <section id="settings-panel-web" role="tabpanel" aria-labelledby="settings-tab-web" hidden={page !== 'web'}>
        <h3 className="text-base font-semibold">网页访问</h3>
        <p className="mt-1 text-sm leading-6 text-slate-500 dark:text-slate-400">允许智能体读取批准网站的文本。作用范围与能力权限一致。</p>
        <fieldset disabled={disabled} aria-label="网页访问策略" className="mt-5 space-y-5">
          <label className="flex cursor-pointer items-start gap-3 border-b border-slate-200 pb-5 dark:border-slate-800">
            <input type="checkbox" role="switch" aria-label="网页读取" checked={selected('web_read')}
              onChange={(event) => onChange(toggleCapability(catalog, draft, 'web_read', event.target.checked))}
              className="mt-1 h-4 w-4 shrink-0 accent-blue-600 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500" />
            <span><span className="block text-sm font-medium">启用网页读取</span><span className="mt-1 block text-sm leading-6 text-slate-500 dark:text-slate-400">仅 HTTPS / 443，不跟随跳转，不携带账号凭据。关闭后保留域名和限额，方便再次启用。</span></span>
          </label>
          <div>
            <label htmlFor="web-domains" className="mb-2 block text-sm font-medium">允许域名</label>
            <textarea id="web-domains" aria-label="允许域名" aria-describedby="web-domain-help" aria-invalid={errorField === 'web-domains'}
              value={draft.domainText} rows={4} maxLength={16384} spellCheck={false}
              placeholder="docs.python.org" onChange={(event) => onChange({ ...draft, domainText: event.target.value })}
              className={`${inputClass} font-mono`} />
            <p id="web-domain-help" className="mt-2 text-sm leading-6 text-slate-500 dark:text-slate-400">每行一个完整域名，不填写网址、路径或通配符。子域名需分别批准；私网、下载和活动内容仍被拒绝。</p>
          </div>
          <details className="group border-t border-slate-200 pt-4 dark:border-slate-800">
            <summary className="cursor-pointer rounded text-sm font-medium focus-visible:outline-2 focus-visible:outline-blue-500">高级限制</summary>
            <div className="mt-4 grid grid-cols-1 gap-4 sm:grid-cols-2">
              {webLimits.map(([key, label, min, max]) => <div key={key}>
                <label htmlFor={`web-${key}`} className="mb-2 block text-sm">{label}</label>
                <input id={`web-${key}`} type="number" aria-label={label} min={min} max={max} step={1}
                  aria-invalid={errorField === `web-${key}`} value={Number.isFinite(draft.webPolicy[key]) ? draft.webPolicy[key] : ''}
                  onChange={(event) => onChange({ ...draft, webPolicy: { ...draft.webPolicy, [key]: event.target.valueAsNumber } })}
                  className={inputClass} />
              </div>)}
            </div>
            <p className="mt-4 text-sm leading-6 text-slate-500 dark:text-slate-400">配额跨服务进程共享，认证范围和用户同时计数。自然分钟窗口；同一数据库硬上限为 8 个并发、240 次/分钟。</p>
          </details>
        </fieldset>
      </section>}
      {!webAvailable && <section id="settings-panel-web" role="tabpanel" aria-labelledby="settings-tab-web" hidden={page !== 'web'}>
        <h3 className="text-base font-semibold">网页访问</h3>
        <p className="mt-2 text-sm leading-6 text-slate-500 dark:text-slate-400">当前服务尚未部署网页读取能力。请先完成服务版本升级，再配置访问权限。</p>
      </section>}
    </>
  )
}
