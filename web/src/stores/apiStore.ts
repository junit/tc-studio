import { create } from 'zustand'
import { readTcStorage, removeTcStorage, writeTcStorage } from '@/lib/storage'

export interface ApiField {
  name: string
  label: string
  type: string
  required: boolean
  default: any
  placeholder: string
  help_text: string
  aliases: string[]
  struct_name?: string
  sub_fields?: ApiField[]
  item_schema?: {
    struct_name?: string
    fields?: ApiField[]
  }
}

export interface ApiMeta {
  api_id: string
  name: string
  service_type?: string
  category: string
  subcategory: string
  path?: string[]
  doc_id?: number
  service_name: string
  command: string
  raw_command?: string
  action?: string
  version?: string
  endpoint?: string
  doc_url: string
  description: string
  fields: ApiField[]
  sample_payload: Record<string, any>
}

export function getCleanApiName(name: string, command?: string, action?: string): string {
  if (!name) return ''
  const target = (action || command || '').trim()
  if (target) {
    const reg = new RegExp(`\\s*[\\(（]${target}[\\)）]\\s*$`, 'i')
    if (reg.test(name)) {
      return name.replace(reg, '').trim()
    }
  }
  return name.replace(/\s*[\(（][a-zA-Z0-9_]+[\)）]\s*$/, '').trim()
}

export interface CloudService {
  id: string
  name: string
  shortName: string
  tag: string
  description: string
  iconName: 'message' | 'video' | 'cloud'
  badgeColor: string
  docUrl?: string
  protocol?: 'rest' | 'api3'
}

export const DEFAULT_CLOUD_SERVICES: CloudService[] = [
  {
    id: 'im',
    name: '即时通信 IM',
    shortName: 'IM',
    tag: 'TIM',
    description: '单聊/群聊/全员推送/会话与资料管理等全量 REST 接口',
    iconName: 'message',
    badgeColor: 'blue',
    docUrl: 'https://cloud.tencent.com/document/product/269/1520',
    protocol: 'rest',
  },
  {
    id: 'live',
    name: '云直播 (CSS)',
    shortName: '云直播',
    tag: 'CSS',
    description: '流管理/导播台/数字人/转码录制等全量 API 3.0 接口',
    iconName: 'video',
    badgeColor: 'emerald',
    docUrl: 'https://cloud.tencent.com/document/product/267/20455',
    protocol: 'api3',
  },
]

export const CLOUD_SERVICES = DEFAULT_CLOUD_SERVICES

export interface InvokeHistoryItem {
  id: string
  timeStr: string
  timestamp: number
  isOk: boolean
  actionStatus: string
  errorCode: number
  costMs: number
  dryRun: boolean
  payload: Record<string, any>
  response: any
}

export interface ApiTab {
  id: string
  apiId: string
  api: ApiMeta
  formData: Record<string, any>
  jsonText: string
  jsonError: string | null
  isModified: boolean
  requestTab: 'params' | 'body' | 'headers' | 'docs'
  bodyMode: 'form' | 'json'
  responseTab: 'pretty' | 'sent' | 'curl' | 'raw'
  invokeResponse: any
  invokeHistory?: InvokeHistoryItem[]
  isInvoking: boolean
  dataVersion?: number
}

export interface ToastMessage {
  id: string
  type?: 'success' | 'info' | 'warning' | 'error'
  message: string
  actionLabel?: string
  onAction?: () => void
}

interface ApiState {
  apis: ApiMeta[]
  activeApi: ApiMeta | null
  tabs: ApiTab[]
  activeTabId: string | null
  toast: ToastMessage | null
  searchQuery: string
  openCategories: Record<string, boolean>
  isInvoking: boolean
  invokeResponse: any
  isSidebarOpen: boolean
  sidebarWidth: number
  isPreviewMode: boolean
  activeServiceId: string
  cloudServices: CloudService[]

  fetchServices: () => Promise<void>
  setActiveServiceId: (serviceId: string) => void
  fetchApis: () => Promise<void>
  setActiveApi: (api: ApiMeta) => void
  openTab: (api: ApiMeta, overrides?: Partial<ApiTab>) => void
  closeTab: (tabId: string) => void
  closeOtherTabs: (tabId: string) => void
  closeTabsToRight: (tabId: string) => void
  closeAllTabs: () => void
  setActiveTabId: (tabId: string) => void
  updateActiveTab: (updates: Partial<ApiTab>) => void
  updateTab: (tabId: string, updates: Partial<ApiTab>) => void
  showToast: (toast: Omit<ToastMessage, 'id'>, duration?: number) => void
  dismissToast: () => void

  setSearchQuery: (query: string) => void
  toggleCategory: (key: string) => void
  expandAll: () => void
  collapseAll: () => void
  toggleSidebar: () => void
  setSidebarOpen: (open: boolean) => void
  setSidebarWidth: (width: number) => void
  setPreviewMode: (enabled: boolean) => void
  togglePreviewMode: () => void
  recentApiIds: string[]
  getRecentApis: () => ApiMeta[]
  getFrequentApis: () => ApiMeta[]
  invokeSingleApi: (apiId: string, payload: Record<string, any>, dryRun?: boolean) => Promise<any>
  clearTabInvokeHistory: (tabId: string) => void
  restoreTabInvokeHistory: (tabId: string, historyItem: InvokeHistoryItem, restorePayload?: boolean) => void
}

// 模糊子序列判定 (如: amw 匹配 admin_msgwithdraw)
function isSubsequence(sub: string, str: string): boolean {
  let s = 0, t = 0
  while (s < sub.length && t < str.length) {
    if (sub[s] === str[t]) s++
    t++
  }
  return s === sub.length
}

/**
 * 智能单输入框多维自适应匹配:
 * 1. 严格全字匹配 (精确命令、ID、中文名)
 * 2. 引号精确搜索 ("admin_msgwithdraw")
 * 3. 连续子串高优先级命中
 * 4. 多词分词并集/交集命中
 * 5. 首字母/子序列缩写匹配 (如 amw, c2c)
 */
export function smartMatchApi(api: ApiMeta, query: string): boolean {
  const q = query.trim().toLowerCase()
  if (!q) return true

  const name = (api.name || '').toLowerCase()
  const cmd = (api.command || '').toLowerCase()
  const id = (api.api_id || '').toLowerCase()
  const desc = (api.description || '').toLowerCase()
  const subcat = (api.subcategory || '').toLowerCase()
  const cat = (api.category || '').toLowerCase()
  const docId = String(api.doc_id || '')

  // 1. 引号精确子串匹配: 例如 "msgwithdraw"
  if (q.startsWith('"') && q.endsWith('"') && q.length > 2) {
    const raw = q.slice(1, -1)
    return name.includes(raw) || cmd.includes(raw) || id.includes(raw) || docId === raw
  }

  // 2. 精确全词或文档 ID 命中
  if (cmd === q || id === q || name === q || subcat === q || cat === q || cmd.endsWith('/' + q) || docId === q) {
    return true
  }

  // 3. 连续子串匹配
  if (cmd.includes(q) || name.includes(q) || id.includes(q) || subcat.includes(q) || cat.includes(q) || desc.includes(q) || docId.includes(q)) {
    return true
  }

  // 4. 多词分词匹配 (空格分隔各词均在语料库中出现)
  const tokens = q.split(/\s+/).filter(Boolean)
  if (tokens.length > 1) {
    const corpus = `${name} ${cmd} ${id} ${desc} ${subcat} ${cat} ${docId}`
    if (tokens.every(token => corpus.includes(token))) {
      return true
    }
  }

  // 5. 缩写/模糊子序列匹配 (如 amw 匹配 admin_msgwithdraw, c2c 匹配 c2c_msg_withdraw)
  if (isSubsequence(q, cmd) || isSubsequence(q, name) || isSubsequence(q, id)) {
    return true
  }

  return false
}

export function initializeFormData(api: ApiMeta): Record<string, any> {
  const result: Record<string, any> = { ...(api.sample_payload || {}) }

  // 针对有定义 fields 的接口，根据 fields 进行字段智能补全与映射
  if (api.fields && api.fields.length > 0) {
    for (const f of api.fields) {
      if (result[f.name] !== undefined) {
        continue
      }
      // 常见字段提取
      if (
        (f.name === 'Text' || f.name === 'content') &&
        api.sample_payload?.MsgBody?.[0]?.MsgContent?.Text
      ) {
        result[f.name] = api.sample_payload.MsgBody[0].MsgContent.Text
      } else if (
        f.name === 'NewText' &&
        api.sample_payload?.MsgBody?.[0]?.MsgContent?.Text
      ) {
        result[f.name] = api.sample_payload.MsgBody[0].MsgContent.Text
      } else if (f.name === 'Text' && api.sample_payload?.Content) {
        result[f.name] = api.sample_payload.Content
      } else if (f.name === 'Tags' && api.sample_payload?.UserTags?.[0]?.Tags) {
        result[f.name] = api.sample_payload.UserTags[0].Tags
      } else if (
        f.name === 'Member_Accounts' &&
        Array.isArray(api.sample_payload?.MemberList)
      ) {
        result[f.name] = api.sample_payload.MemberList.map((m: any) => m.Member_Account)
      } else if (
        f.name === 'Nick' &&
        Array.isArray(api.sample_payload?.ProfileItem)
      ) {
        const item = api.sample_payload.ProfileItem.find(
          (p: any) => p.Tag === 'Tag_Profile_IM_Nick'
        )
        if (item) result[f.name] = item.Value
      } else if (
        f.name === 'FaceUrl' &&
        Array.isArray(api.sample_payload?.ProfileItem)
      ) {
        const item = api.sample_payload.ProfileItem.find(
          (p: any) => p.Tag === 'Tag_Profile_IM_Image'
        )
        if (item) result[f.name] = item.Value
      } else if (
        f.name === 'SelfSignature' &&
        Array.isArray(api.sample_payload?.ProfileItem)
      ) {
        const item = api.sample_payload.ProfileItem.find(
          (p: any) => p.Tag === 'Tag_Profile_IM_SelfSignature'
        )
        if (item) result[f.name] = item.Value
      } else if (
        f.name === 'MsgSeq' &&
        Array.isArray(api.sample_payload?.MsgSeqList)
      ) {
        result[f.name] = api.sample_payload.MsgSeqList[0]?.MsgSeq
      } else if (
        f.name === 'Key' &&
        Array.isArray(api.sample_payload?.ExtensionList)
      ) {
        result[f.name] = api.sample_payload.ExtensionList[0]?.Key
      } else if (
        f.name === 'Value' &&
        Array.isArray(api.sample_payload?.ExtensionList)
      ) {
        result[f.name] = api.sample_payload.ExtensionList[0]?.Value
      } else if (f.default !== undefined && f.default !== null) {
        result[f.name] = f.default
      }
    }
  }
  return result
}

function getLocalApiHistory(apiId: string): InvokeHistoryItem[] {
  try {
    const raw = readTcStorage(localStorage, `api_hist_${apiId}`)
    return raw ? JSON.parse(raw) : []
  } catch {
    return []
  }
}

function saveLocalApiHistory(apiId: string, history: InvokeHistoryItem[]) {
  try {
    writeTcStorage(localStorage, `api_hist_${apiId}`, JSON.stringify(history.slice(0, 15)))
  } catch (e) {
    console.error('Failed to save api history to localStorage', e)
  }
}

function clearLocalApiHistory(apiId: string) {
  try {
    removeTcStorage(localStorage, `api_hist_${apiId}`)
  } catch {}
}

export const CORE_FREQUENT_API_IDS = [
  'c2c_send_msg',          // 单发单聊消息
  'group_send_msg',        // 在群组中发送普通消息
  'group_get_info',        // 获取群详细资料
  'profile_portrait_get',  // 拉取资料
  'group_forbid_send_msg', // 批量禁言和取消禁言
  'c2c_msg_withdraw',      // 撤回单聊消息
]

function getRecentApiIds(): string[] {
  try {
    const raw = readTcStorage(localStorage, 'recent_api_ids')
    return raw ? JSON.parse(raw) : []
  } catch {
    return []
  }
}

function recordRecentApiId(apiId: string) {
  try {
    const list = getRecentApiIds().filter((id) => id !== apiId)
    const updated = [apiId, ...list].slice(0, 20)
    writeTcStorage(localStorage, 'recent_api_ids', JSON.stringify(updated))
  } catch {}
}

interface PersistedTabDraft {
  formData?: Record<string, any>
  jsonText?: string
  isModified?: boolean
  requestTab?: 'params' | 'body' | 'headers' | 'docs'
  bodyMode?: 'form' | 'json'
  responseTab?: 'pretty' | 'sent' | 'curl' | 'raw'
}

interface PersistedTabsState {
  tabIds: string[]
  activeTabId: string | null
  drafts?: Record<string, PersistedTabDraft>
}

function getPersistedTabsState(): PersistedTabsState {
  try {
    const raw = readTcStorage(localStorage, 'open_tabs_state')
    if (raw) {
      const data = JSON.parse(raw)
      return {
        tabIds: Array.isArray(data.tabIds) ? data.tabIds : [],
        activeTabId: data.activeTabId || null,
        drafts: data.drafts || {},
      }
    }
  } catch {}
  return { tabIds: [], activeTabId: null, drafts: {} }
}

function savePersistedTabsState(tabs: ApiTab[], activeTabId: string | null) {
  try {
    const drafts: Record<string, PersistedTabDraft> = {}
    tabs.forEach((t) => {
      drafts[t.apiId] = {
        formData: t.formData,
        jsonText: t.jsonText,
        isModified: t.isModified,
        requestTab: t.requestTab,
        bodyMode: t.bodyMode,
        responseTab: t.responseTab,
      }
    })
    const state: PersistedTabsState = {
      tabIds: tabs.map((t) => t.apiId),
      activeTabId,
      drafts,
    }
    writeTcStorage(localStorage, 'open_tabs_state', JSON.stringify(state))
  } catch {}
}

function getApiCallCounts(): Record<string, number> {
  try {
    const raw = readTcStorage(localStorage, 'api_call_counts')
    return raw ? JSON.parse(raw) : {}
  } catch {
    return {}
  }
}

function incrementApiCallCount(apiId: string) {
  try {
    const counts = getApiCallCounts()
    counts[apiId] = (counts[apiId] || 0) + 1
    writeTcStorage(localStorage, 'api_call_counts', JSON.stringify(counts))
  } catch {}
}

function createTabFromApi(api: ApiMeta): ApiTab {
  const initialData = initializeFormData(api)
  const savedHistory = getLocalApiHistory(api.api_id)
  const lastHistory = savedHistory[0] || null

  return {
    id: api.api_id,
    apiId: api.api_id,
    api,
    formData: initialData,
    jsonText: JSON.stringify(initialData, null, 2),
    jsonError: null,
    isModified: false,
    requestTab: 'body',
    bodyMode: 'form',
    responseTab: 'pretty',
    invokeResponse: lastHistory ? lastHistory.response : null,
    invokeHistory: savedHistory,
    isInvoking: false,
    dataVersion: Date.now(),
  }
}

export const useApiStore = create<ApiState>((set, get) => ({
  apis: [],
  activeApi: null,
  tabs: [],
  activeTabId: (typeof localStorage !== 'undefined' ? getPersistedTabsState().activeTabId : null),
  toast: null,
  searchQuery: '',
  openCategories: {}, // 默认收起全部
  isInvoking: false,
  invokeResponse: null,
  isSidebarOpen: typeof window !== 'undefined' ? window.innerWidth >= 1024 : true, // 大屏默认展开，小屏/移动端默认收起
  sidebarWidth: 320, // 侧边栏固定默认宽度 320px
  isPreviewMode: false,
  activeServiceId: (typeof localStorage !== 'undefined' && readTcStorage(localStorage, 'active_service')) || 'im',
  cloudServices: DEFAULT_CLOUD_SERVICES,
  recentApiIds: getRecentApiIds(),

  fetchServices: async () => {
    try {
      const res = await fetch('/api/services')
      const json = await res.json()
      if (json.status === 'success' && Array.isArray(json.data) && json.data.length > 0) {
        const merged: CloudService[] = json.data.map((srv: any) => {
          const fallback = DEFAULT_CLOUD_SERVICES.find((d) => d.id === srv.id)
          return {
            id: srv.id,
            name: srv.name || fallback?.name || srv.id.toUpperCase(),
            shortName: srv.short_name || srv.shortName || fallback?.shortName || srv.name || srv.id.toUpperCase(),
            tag: srv.tag || fallback?.tag || srv.id.toUpperCase(),
            description: srv.description || fallback?.description || '',
            iconName: (srv.icon_name || fallback?.iconName || (srv.id === 'live' ? 'video' : (srv.id === 'im' ? 'message' : 'cloud'))) as any,
            badgeColor: srv.badge_color || fallback?.badgeColor || (srv.id === 'live' ? 'emerald' : 'blue'),
            docUrl: srv.doc_url || fallback?.docUrl,
            protocol: srv.protocol || fallback?.protocol || (srv.id === 'im' ? 'rest' : 'api3'),
          }
        })
        set({ cloudServices: merged })
      }
    } catch (e) {
      console.warn('Failed to fetch dynamic services, using default fallback', e)
    }
  },

  setActiveServiceId: (serviceId: string) => {
    try {
      writeTcStorage(localStorage, 'active_service', serviceId)
    } catch {}

    const { tabs, apis, activeTabId } = get()
    const targetServiceTabs = tabs.filter((t) => (t.api.service_type || 'im') === serviceId)
    let nextActiveTabId = activeTabId
    let nextActiveApi = get().activeApi
    let newTabs = tabs

    // 若当前激活的 tab 不属于目标服务，切换激活 tab 为目标服务下的 tab
    const currentTab = tabs.find((t) => t.id === activeTabId)
    if (!currentTab || (currentTab.api.service_type || 'im') !== serviceId) {
      if (targetServiceTabs.length > 0) {
        const preferred = targetServiceTabs[targetServiceTabs.length - 1]
        nextActiveTabId = preferred.id
        nextActiveApi = preferred.api
        savePersistedTabsState(tabs, nextActiveTabId)
      } else {
        const defaultApi =
          serviceId === 'live'
            ? apis.find((a) => (a.service_type || 'im') === 'live') || apis[0]
            : apis.find((a) => a.api_id === 'c2c_send_msg') ||
              apis.find((a) => (a.service_type || 'im') === 'im') ||
              apis[0]
        if (defaultApi) {
          const newTab = createTabFromApi(defaultApi)
          newTabs = [...tabs, newTab]
          nextActiveTabId = newTab.id
          nextActiveApi = newTab.api
          savePersistedTabsState(newTabs, nextActiveTabId)
        }
      }
    }

    set({
      tabs: newTabs,
      activeServiceId: serviceId,
      activeTabId: nextActiveTabId,
      activeApi: nextActiveApi,
    })
  },

  getRecentApis: () => {
    const { apis, recentApiIds } = get()
    const list: ApiMeta[] = []
    for (const rid of recentApiIds) {
      const a = apis.find((x) => x.api_id === rid)
      if (a && !list.some((item) => item.api_id === a.api_id)) {
        list.push(a)
      }
    }
    return list
  },

  getFrequentApis: () => {
    const { apis } = get()
    const counts = getApiCallCounts()
    // 找出有实际调用记录且频次最高的接口
    const sortedByUsage = Object.entries(counts)
      .sort((a, b) => b[1] - a[1])
      .map(([id]) => apis.find((x) => x.api_id === id))
      .filter((x): x is ApiMeta => Boolean(x))

    // 结合官方核心常用高频接口
    const coreApis = CORE_FREQUENT_API_IDS.map((id) =>
      apis.find((x) => x.api_id === id)
    ).filter((x): x is ApiMeta => Boolean(x))

    // 合并去重并截取前 6 个
    const result: ApiMeta[] = []
    const seen = new Set<string>()
    for (const a of [...sortedByUsage, ...coreApis]) {
      if (!seen.has(a.api_id)) {
        seen.add(a.api_id)
        result.push(a)
      }
      if (result.length >= 6) break
    }
    return result
  },

  fetchApis: async () => {
    // 异步拉取后端单点真实源 (SSoT) 动态注册的云服务清单
    get().fetchServices().catch(() => {})
    try {
      const res = await fetch('/api/apis')
      const json = await res.json()
      if (json.status === 'success') {
        const rawApis: ApiMeta[] = json.data
        const apis: ApiMeta[] = rawApis.map((a) => ({
          ...a,
          name: getCleanApiName(a.name, a.command, a.action),
        }))
        const persisted = getPersistedTabsState()
        let initialTabs: ApiTab[] = []
        let activeTab: ApiTab | null = null

        // 1. 优先恢复用户上次关闭应用/刷新页面时打开的标签页工作区
        if (persisted.tabIds.length > 0) {
          for (const tid of persisted.tabIds) {
            const matched = apis.find((a) => a.api_id === tid)
            if (matched && !initialTabs.some((t) => t.apiId === matched.api_id)) {
              const tab = createTabFromApi(matched)
              const draft = persisted.drafts?.[tid]
              if (draft) {
                if (draft.formData) tab.formData = draft.formData
                if (draft.jsonText) tab.jsonText = draft.jsonText
                if (draft.isModified !== undefined) tab.isModified = draft.isModified
                if (draft.requestTab) tab.requestTab = draft.requestTab
                if (draft.bodyMode) tab.bodyMode = draft.bodyMode
                if (draft.responseTab) tab.responseTab = draft.responseTab
              }
              initialTabs.push(tab)
            }
          }
          if (persisted.activeTabId) {
            activeTab = initialTabs.find((t) => t.apiId === persisted.activeTabId || t.id === persisted.activeTabId) || null
          }
        }

        // 2. 保持用户在全局服务切换器中选定的权威服务标识 (localStorage 作为 SSoT，绝不被 Tab 的服务类型逆向覆盖)
        const savedService =
          (typeof localStorage !== 'undefined' && readTcStorage(localStorage, 'active_service')) || 'im'
        const activeService = savedService

        // 优先在 initialTabs 中寻找属于 savedService 的激活 Tab
        let serviceActiveTab: ApiTab | null = null
        if (activeTab && (activeTab.api.service_type || 'im') === savedService) {
          serviceActiveTab = activeTab
        } else {
          // 查找已有 initialTabs 中属于当前服务的 Tab
          serviceActiveTab = initialTabs.find((t) => (t.api.service_type || 'im') === savedService) || null
        }

        // 若无属于当前服务的打开标签页，为该服务新建核心默认 Tab
        if (!serviceActiveTab) {
          const defaultApi =
            savedService === 'live'
              ? apis.find((a) => (a.service_type || 'im') === 'live') || apis[0]
              : apis.find((a) => a.api_id === 'c2c_send_msg') ||
                apis.find((a) => (a.service_type || 'im') === 'im') ||
                apis[0]
          if (defaultApi) {
            serviceActiveTab = createTabFromApi(defaultApi)
            initialTabs.push(serviceActiveTab)
          }
        }

        activeTab = serviceActiveTab
        const activeId = activeTab ? activeTab.id : null
        savePersistedTabsState(initialTabs, activeId)

        try {
        writeTcStorage(localStorage, 'active_service', activeService)
        } catch {}

        set({
          apis,
          tabs: initialTabs,
          activeTabId: activeId,
          activeApi: activeTab ? activeTab.api : null,
          activeServiceId: activeService,
          invokeResponse: activeTab?.invokeResponse || null,
          recentApiIds: getRecentApiIds(),
          openCategories: {},
        })
      }
    } catch (e) {
      console.error('Failed to fetch APIs', e)
    }
  },

  setActiveApi: (api: ApiMeta) => {
    get().openTab(api)
  },

  openTab: (api: ApiMeta, overrides?: Partial<ApiTab>) => {
    recordRecentApiId(api.api_id)
    const { tabs } = get()
    const existing = tabs.find(t => t.apiId === api.api_id || t.id === api.api_id)
    let newTabs: ApiTab[]
    let activeId: string
    let activeApi: ApiMeta

    const targetService = api.service_type || 'im'
    if (get().activeServiceId !== targetService) {
      try {
        writeTcStorage(localStorage, 'active_service', targetService)
      } catch {}
    }

    if (existing) {
      // 若原标签页保存的 API 元数据较旧（例如当时 fields 为空，或 payload 为空而新 API 具备有效 payload），进行平滑升级
      const shouldUpdatePayload =
        (!existing.formData || Object.keys(existing.formData).length === 0) &&
        api.sample_payload &&
        Object.keys(api.sample_payload).length > 0

      const freshFormData = shouldUpdatePayload ? initializeFormData(api) : existing.formData
      const freshJsonText = shouldUpdatePayload ? JSON.stringify(freshFormData, null, 2) : existing.jsonText

      const updated = {
        ...existing,
        api, // 升级为最新的包含丰富 fields 的 api 对象
        formData: freshFormData,
        jsonText: freshJsonText,
        ...(overrides || {}),
        dataVersion: Date.now(),
      }
      newTabs = tabs.map(t => (t.id === existing.id || t.apiId === existing.apiId) ? updated : t)
      activeId = existing.id
      activeApi = api
      set({
        tabs: newTabs,
        activeTabId: activeId,
        activeApi: activeApi,
        activeServiceId: targetService,
        invokeResponse: updated.invokeResponse,
        isInvoking: updated.isInvoking,
        recentApiIds: getRecentApiIds(),
      })
    } else {
      const newTab = { ...createTabFromApi(api), ...(overrides || {}), dataVersion: Date.now() }
      newTabs = [...tabs, newTab]
      activeId = newTab.id
      activeApi = api
      set({
        tabs: newTabs,
        activeTabId: activeId,
        activeApi: activeApi,
        activeServiceId: targetService,
        invokeResponse: newTab.invokeResponse,
        isInvoking: false,
        recentApiIds: getRecentApiIds(),
      })
    }
    savePersistedTabsState(newTabs, activeId)
  },

  closeTab: (tabId: string) => {
    const { tabs, activeTabId } = get()
    const tabIndex = tabs.findIndex(t => t.id === tabId)
    if (tabIndex === -1) return

    const newTabs = tabs.filter(t => t.id !== tabId)
    let nextActiveTabId = activeTabId
    let nextActiveApi = get().activeApi

    if (activeTabId === tabId) {
      if (newTabs.length > 0) {
        const nextIndex = Math.max(0, tabIndex - 1)
        const nextTab = newTabs[nextIndex]
        nextActiveTabId = nextTab.id
        nextActiveApi = nextTab.api
      } else {
        nextActiveTabId = null
        nextActiveApi = null
      }
    }

    const currentTab = newTabs.find(t => t.id === nextActiveTabId)
    savePersistedTabsState(newTabs, nextActiveTabId)
    set({
      tabs: newTabs,
      activeTabId: nextActiveTabId,
      activeApi: nextActiveApi,
      invokeResponse: currentTab?.invokeResponse || null,
      isInvoking: currentTab?.isInvoking || false,
    })
  },

  closeOtherTabs: (tabId: string) => {
    const { tabs } = get()
    const keepTab = tabs.find(t => t.id === tabId)
    if (!keepTab) return
    savePersistedTabsState([keepTab], keepTab.id)
    set({
      tabs: [keepTab],
      activeTabId: keepTab.id,
      activeApi: keepTab.api,
      invokeResponse: keepTab.invokeResponse,
      isInvoking: keepTab.isInvoking,
    })
  },

  closeTabsToRight: (tabId: string) => {
    const { tabs, activeTabId } = get()
    const index = tabs.findIndex(t => t.id === tabId)
    if (index === -1) return
    const newTabs = tabs.slice(0, index + 1)
    let nextActiveTabId = activeTabId
    let nextActiveApi = get().activeApi
    if (!newTabs.some(t => t.id === activeTabId)) {
      nextActiveTabId = tabId
      const current = newTabs.find(t => t.id === tabId)
      nextActiveApi = current ? current.api : null
    }
    const currentTab = newTabs.find(t => t.id === nextActiveTabId)
    savePersistedTabsState(newTabs, nextActiveTabId)
    set({
      tabs: newTabs,
      activeTabId: nextActiveTabId,
      activeApi: nextActiveApi,
      invokeResponse: currentTab?.invokeResponse || null,
      isInvoking: currentTab?.isInvoking || false,
    })
  },

  closeAllTabs: () => {
    savePersistedTabsState([], null)
    set({
      tabs: [],
      activeTabId: null,
      activeApi: null,
      invokeResponse: null,
      isInvoking: false,
    })
  },

  setActiveTabId: (tabId: string) => {
    const { tabs } = get()
    const target = tabs.find(t => t.id === tabId || t.apiId === tabId)
    if (target) {
      const targetService = target.api.service_type || 'im'
      if (get().activeServiceId !== targetService) {
        try {
        writeTcStorage(localStorage, 'active_service', targetService)
        } catch {}
      }

      savePersistedTabsState(tabs, target.id)
      set({
        activeTabId: target.id,
        activeApi: target.api,
        activeServiceId: targetService,
        invokeResponse: target.invokeResponse,
        isInvoking: target.isInvoking,
      })
    }
  },

  updateActiveTab: (updates: Partial<ApiTab>) => {
    const { activeTabId, tabs } = get()
    if (!activeTabId) return
    const newTabs = tabs.map(t => {
      if (t.id === activeTabId) {
        return { ...t, ...updates }
      }
      return t
    })
    savePersistedTabsState(newTabs, activeTabId)
    set({ tabs: newTabs })
  },

  updateTab: (tabId: string, updates: Partial<ApiTab>) => {
    const { tabs, activeTabId } = get()
    const newTabs = tabs.map(t => {
      if (t.id === tabId) {
        return { ...t, ...updates }
      }
      return t
    })
    savePersistedTabsState(newTabs, activeTabId)
    set({ tabs: newTabs })
  },

  showToast: (toast: Omit<ToastMessage, 'id'>, duration: number = 3500) => {
    const id = Math.random().toString(36).slice(2, 9)
    set({ toast: { ...toast, id } })
    if (duration > 0) {
      setTimeout(() => {
        if (get().toast?.id === id) {
          set({ toast: null })
        }
      }, duration)
    }
  },

  dismissToast: () => set({ toast: null }),

  setSearchQuery: (query: string) => set({ searchQuery: query }),

  toggleCategory: (key: string) => {
    set((state) => ({
      openCategories: {
        ...state.openCategories,
        [key]: !state.openCategories[key],
      },
    }))
  },

  expandAll: () => {
    const { apis } = get()
    const all: Record<string, boolean> = {}
    apis.forEach((a) => {
      const parts = (a.path && a.path.length > 0)
        ? [...a.path]
        : [a.category, a.subcategory].filter(Boolean) as string[]
      let cur = ''
      parts.forEach((p) => {
        cur = cur ? `${cur}:::${p}` : p
        all[cur] = true
      })

      // 同时注册去除顶层业务名（如 '云直播 (CSS)'、'即时通信 (IM)' 等）后的路径键，确保侧边栏树结构展开兼容
      if (parts.length > 1 && (parts[0].includes('云直播') || parts[0].includes('CSS') || parts[0].includes('IM') || parts[0].includes('即时通信'))) {
        const stripped = parts.slice(1)
        let curStripped = ''
        stripped.forEach((p) => {
          curStripped = curStripped ? `${curStripped}:::${p}` : p
          all[curStripped] = true
        })
      }
    })
    set({ openCategories: all })
  },

  collapseAll: () => {
    set({ openCategories: {} })
  },

  toggleSidebar: () => {
    set((state) => ({ isSidebarOpen: !state.isSidebarOpen }))
  },

  setSidebarOpen: (open: boolean) => {
    set({ isSidebarOpen: open })
  },

  setSidebarWidth: (width: number) => {
    set({ sidebarWidth: Math.max(260, Math.min(560, width)) })
  },

  setPreviewMode: (enabled: boolean) => set({ isPreviewMode: enabled }),

  togglePreviewMode: () => set((state) => ({ isPreviewMode: !state.isPreviewMode })),

  invokeSingleApi: async (apiId: string, payload: Record<string, any>, dryRun?: boolean) => {
    incrementApiCallCount(apiId)
    const isDry = dryRun !== undefined ? dryRun : get().isPreviewMode
    const { activeTabId } = get()
    if (activeTabId) {
      get().updateTab(activeTabId, { isInvoking: true })
    }
    set({ isInvoking: true, invokeResponse: null })
    try {
      const res = await fetch(`/api/apis/${apiId}/invoke`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ payload, dry_run: isDry }),
      })
      const data = await res.json()
      const now = new Date()
      const timeStr =
        now.toTimeString().split(' ')[0] +
        '.' +
        String(now.getMilliseconds()).padStart(3, '0')
      const isOk =
        Boolean(data.dry_run) ||
        (data.response?.ActionStatus === 'OK' &&
          (data.response?.ErrorCode === 0 || data.response?.ErrorCode === undefined))
      const historyItem: InvokeHistoryItem = {
        id: Math.random().toString(36).substring(2, 9),
        timeStr,
        timestamp: Date.now(),
        isOk,
        actionStatus: data.dry_run
          ? 'PREVIEW'
          : data.response?.ActionStatus || (isOk ? 'OK' : 'FAIL'),
        errorCode: data.response?.ErrorCode ?? (isOk ? 0 : -1),
        costMs: data.cost_ms ?? 0,
        dryRun: Boolean(data.dry_run),
        payload: payload || {},
        response: data,
      }

      if (activeTabId) {
        const currentTab = get().tabs.find((t) => t.id === activeTabId)
        const prevHistory = currentTab?.invokeHistory || []
        const newHistory = [historyItem, ...prevHistory.filter(h => h.id !== historyItem.id).slice(0, 14)]
        if (currentTab) {
          saveLocalApiHistory(currentTab.apiId, newHistory)
        }
        get().updateTab(activeTabId, {
          invokeResponse: data,
          invokeHistory: newHistory,
          isInvoking: false,
        })
      }
      set({ invokeResponse: data, isInvoking: false })
      return data
    } catch (e: any) {
      const errRes = { status: 'error', message: e.message || String(e) }
      const now = new Date()
      const timeStr =
        now.toTimeString().split(' ')[0] +
        '.' +
        String(now.getMilliseconds()).padStart(3, '0')
      const historyItem: InvokeHistoryItem = {
        id: Math.random().toString(36).substring(2, 9),
        timeStr,
        timestamp: Date.now(),
        isOk: false,
        actionStatus: 'ERROR',
        errorCode: -1,
        costMs: 0,
        dryRun: Boolean(isDry),
        payload: payload || {},
        response: errRes,
      }
      if (activeTabId) {
        const currentTab = get().tabs.find((t) => t.id === activeTabId)
        const prevHistory = currentTab?.invokeHistory || []
        const newHistory = [historyItem, ...prevHistory.filter(h => h.id !== historyItem.id).slice(0, 14)]
        if (currentTab) {
          saveLocalApiHistory(currentTab.apiId, newHistory)
        }
        get().updateTab(activeTabId, {
          invokeResponse: errRes,
          invokeHistory: newHistory,
          isInvoking: false,
        })
      }
      set({ invokeResponse: errRes, isInvoking: false })
      return errRes
    }
  },

  clearTabInvokeHistory: (tabId: string) => {
    const currentTab = get().tabs.find((t) => t.id === tabId)
    if (currentTab) {
      clearLocalApiHistory(currentTab.apiId)
    }
    get().updateTab(tabId, { invokeHistory: [] })
  },

  restoreTabInvokeHistory: (tabId: string, historyItem: InvokeHistoryItem, restorePayload = false) => {
    const currentTab = get().tabs.find((t) => t.id === tabId)
    const updates: Partial<ApiTab> = {
      invokeResponse: historyItem.response,
    }
    if (restorePayload && historyItem.payload) {
      updates.formData = historyItem.payload
      updates.jsonText = JSON.stringify(historyItem.payload, null, 2)
      updates.dataVersion = (currentTab?.dataVersion || 0) + 1
    }
    get().updateTab(tabId, updates)
    set({ invokeResponse: historyItem.response })
  },
}))
