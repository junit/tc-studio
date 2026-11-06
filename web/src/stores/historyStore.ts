import { create } from 'zustand'

const HISTORY_DETAIL_PAGE_SIZE = 5000
const HISTORY_DETAIL_MAX_PAGES = 20

export interface HistoryItem {
  task_id: string
  api_id: string
  api_name?: string
  service_type?: string
  total: number
  success: number
  failed: number
  duration_sec: number
  status: string
  created_at: string
  result_count?: number
  result_offset?: number
  result_limit?: number
  result_total?: number
  result_has_more?: boolean
  results?: any[]
}

interface HistoryState {
  historyList: HistoryItem[]
  selectedTask: HistoryItem | null
  isLoading: boolean
  isDetailLoading: boolean

  fetchHistory: () => Promise<void>
  fetchDetail: (taskId: string, loadAll?: boolean) => Promise<void>
  clearSelected: () => void
  deleteHistory: (taskId: string) => Promise<boolean>
  clearAllHistory: () => Promise<boolean>
}

export const useHistoryStore = create<HistoryState>((set, get) => {
  let detailAbortController: AbortController | null = null

  return {
    historyList: [],
    selectedTask: null,
    isLoading: false,
    isDetailLoading: false,

  fetchHistory: async () => {
    set({ isLoading: true })
    try {
      const res = await fetch('/api/history')
      const json = await res.json()
      if (json.status === 'success') {
        set({ historyList: json.data || [], isLoading: false })
      } else {
        set({ isLoading: false })
      }
    } catch (e) {
      console.error('Fetch history failed', e)
      set({ isLoading: false })
    }
  },

    fetchDetail: async (taskId: string, loadAll = false) => {
      // A newly selected history item supersedes an in-flight detail request.
      detailAbortController?.abort()
      const controller = new AbortController()
      detailAbortController = controller
      set({ isDetailLoading: true })

      try {
        const pageSize = HISTORY_DETAIL_PAGE_SIZE
        let offset = 0
        let detail: HistoryItem | null = null
        const maxPages = loadAll ? HISTORY_DETAIL_MAX_PAGES : 1

        for (let pageNumber = 0; pageNumber < maxPages; pageNumber += 1) {
          const query = loadAll ? `?offset=${offset}&limit=${pageSize}` : ''
          const res = await fetch(`/api/history/${taskId}${query}`, { signal: controller.signal })
          // AbortController cancellation can race a response that already
          // resolved, so check ownership before allowing stale data to win.
          if (controller.signal.aborted || detailAbortController !== controller) return

          const json = await res.json()
          if (controller.signal.aborted || detailAbortController !== controller) return
          if (json.status !== 'success') return

          const page = json.data as HistoryItem
          const results = Array.isArray(page.results) ? page.results : []
          detail = detail
            ? { ...detail, ...page, results: [...(detail.results || []), ...results] }
            : { ...page, results }

          if (!loadAll || !page.result_has_more) break
          offset += results.length || pageSize
        }

        if (detailAbortController === controller) set({ selectedTask: detail })
      } catch (e) {
        if (!controller.signal.aborted) console.error('Fetch detail failed', e)
      } finally {
        if (detailAbortController === controller) {
          detailAbortController = null
          set({ isDetailLoading: false })
        }
      }
    },

  deleteHistory: async (taskId: string) => {
    try {
      const res = await fetch(`/api/history/${taskId}`, { method: 'DELETE' })
      const json = await res.json()
      if (json.status === 'success') {
        set((state) => ({
          historyList: state.historyList.filter((h) => h.task_id !== taskId),
          selectedTask: state.selectedTask?.task_id === taskId ? null : state.selectedTask,
        }))
        return true
      }
    } catch (e) {
      console.error('Delete history failed', e)
    }
    return false
  },

  clearAllHistory: async () => {
    try {
      const res = await fetch('/api/history', { method: 'DELETE' })
      const json = await res.json()
      if (json.status === 'success') {
        set({ historyList: [], selectedTask: null })
        return true
      }
    } catch (e) {
      console.error('Clear all history failed', e)
    }
    return false
  },

    clearSelected: () => set({ selectedTask: null }),
  }
})
