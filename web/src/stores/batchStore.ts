import { create } from 'zustand'

export interface LogItem {
  id: string
  time: string
  tag: 'SUCCESS' | 'FAILED' | 'INFO' | 'WARN'
  msg: string
  cost?: number
}

export interface BatchPreset {
  id: string
  service_type?: string
  title: string
  tag: string
  tag_color: string
  api_id: string
  command: string
  description: string
  filename: string
  columns: string[]
  recommended_delay: number
  sample_rows: any[]
  has_today_data?: boolean
}

export interface BatchGuidance {
  api_id: string
  rate_limit: string
  risk_level: 'NORMAL' | 'HIGH_RISK' | 'SINGLETON' | 'AI_EXPENSIVE'
  min_delay: number
  recommended_delay: number
  native_alternative?: {
    api_id: string
    name: string
    command: string
  } | null
  tip?: string | null
}

interface BatchState {
  apiId: string
  items: any[]
  fileName: string
  delaySec: number
  isDryRun: boolean
  activeTaskId: string | null
  isRunning: boolean
  isPaused: boolean
  progress: {
    current: number
    total: number
    percent: number
    success: number
    failed: number
  }
  logs: LogItem[]
  presets: BatchPreset[]
  activePresetId: string | null
  guidance: BatchGuidance | null
  failedItems: any[]

  setApiId: (id: string) => void
  setDelaySec: (sec: number) => void
  setDryRun: (dry: boolean) => void
  toggleDryRun: () => void
  setItems: (items: any[], fileName: string) => void
  clearItems: () => void
  loadTodayPreset: () => Promise<void>
  loadPresets: () => Promise<void>
  applyPreset: (preset: BatchPreset, loadSampleData?: boolean) => Promise<void>
  fetchGuidance: (apiId: string) => Promise<void>
  retryFailed: () => void
  addLog: (tag: LogItem['tag'], msg: string, cost?: number) => void
  clearLogs: () => void
  startBatch: () => Promise<void>
  connectSSE: (taskId: string) => void
  pauseBatch: () => Promise<void>
  resumeBatch: () => Promise<void>
  stopBatch: () => Promise<void>
}

let eventSource: EventSource | null = null

export const useBatchStore = create<BatchState>((set, get) => ({
  apiId: 'c2c_msg_withdraw',
  items: [],
  fileName: '',
  delaySec: 0.05,
  isDryRun: false,
  activeTaskId: null,
  isRunning: false,
  isPaused: false,
  progress: {
    current: 0,
    total: 0,
    percent: 0,
    success: 0,
    failed: 0,
  },
  logs: [],
  presets: [],
  activePresetId: null,
  guidance: null,
  failedItems: [],

  setApiId: (id: string) => {
    set({ apiId: id, activePresetId: null })
    get().fetchGuidance(id)
  },
  setDelaySec: (sec: number) => set({ delaySec: sec }),
  setDryRun: (dry: boolean) => set({ isDryRun: dry }),
  toggleDryRun: () => set((state) => ({ isDryRun: !state.isDryRun })),
  setItems: (items: any[], fileName: string) =>
    set({
      items,
      fileName,
      failedItems: [],
      progress: { current: 0, total: items.length, percent: 0, success: 0, failed: 0 },
    }),

  clearItems: () =>
    set({
      items: [],
      fileName: '',
      failedItems: [],
      progress: { current: 0, total: 0, percent: 0, success: 0, failed: 0 },
    }),

  loadPresets: async () => {
    try {
      const res = await fetch('/api/batch/presets')
      const json = await res.json()
      if (json.status === 'success') {
        set({ presets: json.data })
      }
    } catch (e: any) {
      console.warn('Failed to load batch presets:', e)
    }
  },

  applyPreset: async (preset: BatchPreset, loadSampleData = true) => {
    set({
      apiId: preset.api_id,
      activePresetId: preset.id,
      delaySec: preset.recommended_delay,
    })
    get().fetchGuidance(preset.api_id)

    if (loadSampleData && preset.sample_rows) {
      get().setItems(preset.sample_rows, preset.filename)
      get().addLog(
        'SUCCESS',
        `已载入预设「${preset.title}」的演示样本 (${preset.sample_rows.length} 条记录)`
      )
    }
  },

  fetchGuidance: async (apiId: string) => {
    try {
      const res = await fetch(`/api/batch/guidance/${apiId}`)
      const json = await res.json()
      if (json.status === 'success') {
        const g: BatchGuidance = json.data
        set({ guidance: g })
        if (g.min_delay && get().delaySec < g.min_delay) {
          set({ delaySec: g.recommended_delay })
          get().addLog(
            'WARN',
            `根据官方文档频控限制，已自动将执行间隔保护性调整为 ${g.recommended_delay}s`
          )
        }
      }
    } catch (e: any) {
      console.warn('Failed to load guidance:', e)
    }
  },

  retryFailed: () => {
    const { failedItems, fileName } = get()
    if (failedItems.length === 0) return
    const retryFileName = fileName
      ? `${fileName.replace(/(_failed_retry)?(\.csv|\.json)?$/, '')}_failed_retry.csv`
      : 'failed_retry.csv'
    const count = failedItems.length
    get().setItems(failedItems, retryFileName)
    get().addLog(
      'WARN',
      `已自动提取 ${count} 条失败记录载入待执行区，可检查后直接点击「启动批处理」重试`
    )
  },

  addLog: (tag, msg, cost) => {
    const now = new Date()
    const time =
      now.toTimeString().split(' ')[0] +
      '.' +
      String(now.getMilliseconds()).padStart(3, '0')
    const item: LogItem = {
      id: Math.random().toString(36).substring(2, 9),
      time,
      tag,
      msg,
      cost,
    }
    set((state) => ({ logs: [...state.logs.slice(-200), item] }))
  },

  clearLogs: () => set({ logs: [] }),

  loadTodayPreset: async () => {
    const firstPreset = get().presets[0]
    if (firstPreset) {
      await get().applyPreset(firstPreset, true)
    }
  },

  startBatch: async () => {
    const { items, apiId, delaySec, isDryRun } = get()
    if (items.length === 0) return

    set({
      isRunning: true,
      isPaused: false,
      failedItems: [],
      progress: { current: 0, total: items.length, percent: 0, success: 0, failed: 0 },
    })
    get().addLog(
      'INFO',
      `启动批量${isDryRun ? '【预览演练】' : ''}任务，目标接口: ${apiId}，总记录: ${items.length} 条`
    )

    try {
      const res = await fetch('/api/batch/create', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          api_id: apiId,
          items,
          delay_sec: delaySec,
          dry_run: isDryRun,
        }),
      })
      const json = await res.json()
      if (json.status === 'success') {
        set({ activeTaskId: json.task_id })
        get().connectSSE(json.task_id)
      } else {
        alert(json.detail || '创建批处理任务失败')
        set({ isRunning: false })
      }
    } catch (e: any) {
      alert('请求失败: ' + e.message)
      set({ isRunning: false })
    }
  },

  connectSSE: (taskId: string) => {
    if (eventSource) eventSource.close()
    const source = new EventSource(`/api/batch/${taskId}/events`)
    eventSource = source
    let reconnectLogged = false
    const isCurrentSource = () => eventSource === source && get().activeTaskId === taskId

    source.onmessage = (e) => {
      if (!isCurrentSource() || !e.data || e.data.startsWith(':')) return
      let evt: { type: string; data: any }
      try {
        evt = JSON.parse(e.data)
      } catch (error) {
        console.warn('Invalid batch SSE payload', error)
        return
      }
      const { type, data } = evt

      if (type === 'start') {
        get().addLog('INFO', `批次 [${data.task_id}] 正式开始执行...`)
      } else if (type === 'progress') {
        set({
          progress: {
            current: data.current,
            total: data.total,
            percent: data.percent,
            success: data.success,
            failed: data.failed,
          },
        })
        if (!data.is_ok && data.item) {
          const rawRow = { ...data.item }
          delete rawRow._status
          delete rawRow._err_code
          delete rawRow._err_info
          delete rawRow._cost_ms
          set((state) => ({ failedItems: [...state.failedItems, rawRow] }))
        }
        const tag = data.is_ok ? 'SUCCESS' : 'FAILED'
        const target =
          data.item.to_account || data.item.To_Account || data.item.GroupId || ''
        const key =
          data.item.msg_key || data.item.MsgKey || data.item.MsgSeq || ''
        const msg = `[${data.current}/${data.total}] Target: ${target} | Key/Seq: ${key} | Code: ${data.err_code} ${
          data.err_info ? '(' + data.err_info + ')' : ''
        }`
        get().addLog(tag, msg, data.cost_ms)
      } else if (type === 'complete') {
        set({ isRunning: false })
        get().addLog(
          'INFO',
          `🎉 批处理完成！耗时: ${data.duration_sec}s | 成功: ${data.success} | 失败: ${data.failed}`
        )
        if (eventSource === source) {
          source.close()
          eventSource = null
        }
      } else if (type === 'error') {
        set({ isRunning: false })
        get().addLog('FAILED', `任务错误: ${data.message}`)
        if (eventSource === source) {
          source.close()
          eventSource = null
        }
      }
    }

    source.onerror = () => {
      if (!isCurrentSource()) {
        source.close()
        return
      }
      // EventSource automatically retries transient disconnects. Keep the task running
      // until the browser reports CLOSED or the server sends an explicit terminal event.
      if (source.readyState === EventSource.CLOSED) {
        set({ isRunning: false })
        get().addLog('FAILED', 'SSE 连接已关闭，批处理仍可能在后台运行')
        source.close()
        eventSource = null
      } else if (!reconnectLogged) {
        reconnectLogged = true
        get().addLog('WARN', 'SSE 连接暂时中断，正在自动重连...')
      }
    }
  },

  pauseBatch: async () => {
    const { activeTaskId } = get()
    if (!activeTaskId) return
    await fetch(`/api/batch/${activeTaskId}/pause`, { method: 'POST' })
    set({ isPaused: true })
    get().addLog('WARN', '批量任务已暂停')
  },

  resumeBatch: async () => {
    const { activeTaskId } = get()
    if (!activeTaskId) return
    await fetch(`/api/batch/${activeTaskId}/resume`, { method: 'POST' })
    set({ isPaused: false })
    get().addLog('INFO', '批量任务继续执行')
  },

  stopBatch: async () => {
    const { activeTaskId } = get()
    if (!activeTaskId) return
    if (!confirm('确定要终止当前批处理任务吗？')) return
    try {
      const response = await fetch(`/api/batch/${activeTaskId}/stop`, { method: 'POST' })
      const result = await response.json()
      if (!response.ok || !['CANCELLED', 'COMPLETED', 'FAILED'].includes(result.task_status)) {
        throw new Error(result.detail || '后端未确认任务终止')
      }
      set({ isRunning: result.task_status === 'RUNNING' })
      if (eventSource) {
        eventSource.close()
        eventSource = null
      }
      get().addLog('WARN', '批量任务已被用户强制终止')
    } catch (error: any) {
      get().addLog('FAILED', `终止任务失败: ${error.message}`)
    }
  },
}))
