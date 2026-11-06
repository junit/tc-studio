import React, { useEffect, useState, useMemo, useRef } from 'react'
import {
  History,
  Download,
  Eye,
  CheckCircle2,
  AlertTriangle,
  Clock,
  Layers,
  FileSpreadsheet,
  X,
  RefreshCw,
  Trash2,
  Play,
  ArrowRight,
  Search,
  RotateCcw,
  Sparkles,
  ChevronDown,
} from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Card, CardHeader, CardTitle, CardContent } from '@/components/ui/card'
import { useHistoryStore, HistoryItem } from '@/stores/historyStore'
import { useBatchStore } from '@/stores/batchStore'
import { useApiStore } from '@/stores/apiStore'
import { downloadWithAuth } from '@/lib/auth'

interface HistoryPanelProps {
  onSwitchToBatch?: () => void
  isEmbedded?: boolean
}

interface ExportDropdownProps {
  taskId: string
  total: number
  failed: number
}

const ExportDropdown: React.FC<ExportDropdownProps> = ({ taskId, total, failed }) => {
  const [isOpen, setIsOpen] = useState(false)
  const dropdownRef = useRef<HTMLDivElement>(null)
  const showToast = useApiStore((state) => state.showToast)

  useEffect(() => {
    const handleClickOutside = (e: MouseEvent) => {
      if (dropdownRef.current && !dropdownRef.current.contains(e.target as Node)) {
        setIsOpen(false)
      }
    }
    if (isOpen) {
      document.addEventListener('mousedown', handleClickOutside)
    }
    return () => document.removeEventListener('mousedown', handleClickOutside)
  }, [isOpen])

  return (
    <div className="relative" ref={dropdownRef}>
      <Button
        variant="secondary"
        size="sm"
        onClick={() => setIsOpen(!isOpen)}
        className="h-8 gap-1 px-2.5 text-xs text-slate-700 dark:text-slate-300 hover:bg-slate-200 dark:bg-slate-800 dark:hover:bg-slate-700 cursor-pointer shrink-0"
        title="选择导出 CSV 模式"
      >
        <Download className="h-3.5 w-3.5" />
        <span>导出 CSV</span>
        <ChevronDown className="h-3 w-3 opacity-60" />
      </Button>

      {isOpen && (
        <div className="absolute right-0 top-full mt-1.5 w-48 rounded-xl border border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900 shadow-xl z-30 p-1 space-y-0.5 animate-in fade-in-50 zoom-in-95 duration-100">
          <button
            type="button"
            onClick={() => {
              downloadWithAuth(`/api/batch/${taskId}/export/csv`, `${taskId}.csv`).catch((error) => showToast({ type: 'error', message: error.message || '导出失败' }))
              setIsOpen(false)
            }}
            className="w-full text-left flex items-center justify-between px-2.5 py-1.5 rounded-lg text-xs hover:bg-slate-100 dark:hover:bg-slate-800 text-slate-700 dark:text-slate-200 cursor-pointer"
          >
            <span>导出全量对账表</span>
            <span className="font-mono text-[10px] text-slate-400">({total}条)</span>
          </button>

          {failed > 0 && (
            <button
              type="button"
              onClick={() => {
                downloadWithAuth(`/api/batch/${taskId}/export/csv?only_failed=true`, `${taskId}_failed.csv`).catch((error) => showToast({ type: 'error', message: error.message || '导出失败' }))
                setIsOpen(false)
              }}
              className="w-full text-left flex items-center justify-between px-2.5 py-1.5 rounded-lg text-xs hover:bg-rose-50 dark:hover:bg-rose-950/40 text-rose-600 dark:text-rose-400 cursor-pointer font-medium"
            >
              <span>仅导出失败项</span>
              <span className="font-mono text-[10px]">({failed}条)</span>
            </button>
          )}
        </div>
      )}
    </div>
  )
}

export const HistoryPanel: React.FC<HistoryPanelProps> = ({
  onSwitchToBatch,
  isEmbedded = false,
}) => {
  const {
    historyList,
    selectedTask,
    isLoading,
    isDetailLoading,
    fetchHistory,
    fetchDetail,
    deleteHistory,
    clearAllHistory,
    clearSelected,
  } = useHistoryStore()

  const showToast = useApiStore((state) => state.showToast)

  // 详情弹窗内的筛选状态
  const [filterStatus, setFilterStatus] = useState<'ALL' | 'FAILED' | 'SUCCESS'>('ALL')
  const [searchTerm, setSearchTerm] = useState<string>('')

  useEffect(() => {
    fetchHistory()
  }, [fetchHistory])

  // 导出 CSV（全量或仅失败）
  const handleExport = (taskId: string, onlyFailed = false) => {
    downloadWithAuth(
      `/api/batch/${taskId}/export/csv${onlyFailed ? '?only_failed=true' : ''}`,
      `${taskId}${onlyFailed ? '_failed' : ''}.csv`
    ).catch((error) => showToast({ type: 'error', message: error.message || '导出失败' }))
  }

  // 删除单条历史记录
  const handleDeleteTask = async (e: React.MouseEvent, taskId: string) => {
    e.stopPropagation()
    if (!confirm(`确定要删除历史记录 [${taskId}] 吗？`)) return
    await deleteHistory(taskId)
  }

  // 清空全部历史
  const handleClearAll = async () => {
    if (historyList.length === 0) return
    if (!confirm('确定要清空所有历史归档台账吗？此操作不可恢复。')) return
    await clearAllHistory()
  }

  // 一键重载到 Runner 批量调用测试中
  const handleLoadIntoRunner = async (task: HistoryItem) => {
    let taskToLoad = task
    // 如果列表中的摘要没有 results，先拉取完整详情
    if (!task.results || task.results.length === 0) {
      await fetchDetail(task.task_id, true)
      const currentSelected = useHistoryStore.getState().selectedTask
      if (currentSelected && currentSelected.results) {
        taskToLoad = currentSelected
      }
    }

    if (!taskToLoad.results || taskToLoad.results.length === 0) {
      showToast({ type: 'error', message: '该历史记录未包含有效的数据行' })
      return
    }

    // 剔除执行元数据字段，还原干净的原始入参
    const cleanItems = taskToLoad.results.map((row: any) => {
      const clean = { ...row }
      delete clean._status
      delete clean._err_code
      delete clean._err_info
      delete clean._cost_ms
      delete clean.status
      delete clean.err_code
      delete clean.err_info
      delete clean.cost_ms
      return clean
    })

    const batchStore = useBatchStore.getState()
    batchStore.setItems(cleanItems, `history_${taskToLoad.task_id}.csv`)
    batchStore.setApiId(taskToLoad.api_id)
    batchStore.addLog(
      'SUCCESS',
      `已从历史台账 [${taskToLoad.task_id}] 还原 ${cleanItems.length} 条数据，目标接口已设定为: ${
        taskToLoad.api_name || taskToLoad.api_id
      }`
    )

    clearSelected()
    if (onSwitchToBatch) {
      onSwitchToBatch()
    }
  }

  // 动态自适应提取业务参数列（除 _status, _err_code, _err_info, _cost_ms 以外的所有列）
  const dynamicParamColumns = useMemo(() => {
    if (!selectedTask?.results || selectedTask.results.length === 0) return []
    const reserved = new Set([
      '_status',
      '_err_code',
      '_err_info',
      '_cost_ms',
      'status',
      'err_code',
      'err_info',
      'cost_ms',
    ])
    const colSet = new Set<string>()
    selectedTask.results.slice(0, 30).forEach((r: any) => {
      Object.keys(r).forEach((k) => {
        if (!reserved.has(k) && !k.startsWith('_')) {
          colSet.add(k)
        }
      })
    })
    return Array.from(colSet)
  }, [selectedTask])

  // 详情结果行过滤（状态 Tab + 搜索关键字）
  const filteredResults = useMemo(() => {
    if (!selectedTask?.results) return []
    return selectedTask.results.filter((row: any) => {
      // 规范化取值（修复 _status / _err_code 错位问题）
      const status = row._status || row.status || 'OK'
      const errCode =
        row._err_code !== undefined
          ? row._err_code
          : row.err_code !== undefined
          ? row.err_code
          : 0
      const isOk = status === 'OK' && Number(errCode) === 0

      if (filterStatus === 'FAILED' && isOk) return false
      if (filterStatus === 'SUCCESS' && !isOk) return false

      if (searchTerm.trim()) {
        const query = searchTerm.toLowerCase().trim()
        const matchString = Object.values(row)
          .map((v) => String(v ?? '').toLowerCase())
          .join(' ')
        if (!matchString.includes(query)) return false
      }

      return true
    })
  }, [selectedTask, filterStatus, searchTerm])

  return (
    <div className="flex h-full flex-col bg-slate-50/60 dark:bg-slate-950 overflow-hidden">
      {/* Top Header */}
      <div
        className={`shrink-0 flex flex-wrap items-center justify-between gap-3 sm:gap-4 border-b border-slate-200/80 dark:border-slate-800 bg-white dark:bg-slate-900 px-4 sm:px-6 ${
          isEmbedded ? 'py-2.5 min-h-[52px]' : 'py-2.5 min-h-[56px]'
        } shadow-2xs select-none`}
      >
        <div className="flex items-center gap-3">
          <div className="flex h-8 w-8 items-center justify-center rounded-xl bg-blue-50 dark:bg-blue-950/60 text-blue-600 dark:text-blue-400 border border-blue-200 dark:border-blue-800/80 shadow-2xs shrink-0">
            <History className="h-4 w-4" />
          </div>
          <div className="min-w-0">
            {!isEmbedded ? (
              <div className="flex items-center gap-2">
                <h2 className="text-xs font-bold text-slate-900 dark:text-slate-100 truncate">
                  执行审计台账与调用历史
                </h2>
                <Badge
                  variant="outline"
                  className="bg-blue-50 dark:bg-blue-950/60 text-blue-700 dark:text-blue-400 border-blue-200 dark:border-blue-800 font-mono text-[9px] py-0 px-1.5 shrink-0"
                >
                  Audit
                </Badge>
              </div>
            ) : (
              <div className="flex items-center gap-2">
                <h2 className="text-xs font-bold text-slate-900 dark:text-slate-100 truncate">
                  历史任务归档记录
                </h2>
                <Badge
                  variant="outline"
                  className="bg-blue-50 dark:bg-blue-950/60 text-blue-700 dark:text-blue-400 border-blue-200 dark:border-blue-800 font-mono text-[9px] py-0 px-1.5 shrink-0"
                >
                  共 {historyList.length} 批次
                </Badge>
              </div>
            )}
            <p className="text-[10px] text-slate-500 dark:text-slate-400 truncate">
              本地持久化记录所有批处理任务调用台账、成功率统计与各行响应回执（自动保留最近 50 次）
            </p>
          </div>
        </div>

        <div className="flex items-center gap-2.5">
          {historyList.length > 0 && (
            <Button
              variant="ghost"
              size="sm"
              onClick={handleClearAll}
              className="gap-1 text-xs text-rose-600 hover:text-rose-700 hover:bg-rose-50 dark:text-rose-400 dark:hover:bg-rose-950/40 cursor-pointer"
            >
              <Trash2 className="h-3.5 w-3.5" />
              <span>清空所有历史</span>
            </Button>
          )}

          <Button
            variant="outline"
            size="sm"
            onClick={() => fetchHistory()}
            disabled={isLoading}
            className="gap-1.5 text-xs text-slate-600 dark:text-slate-300 cursor-pointer"
          >
            <RefreshCw className={`h-3.5 w-3.5 ${isLoading ? 'animate-spin' : ''}`} />
            <span>刷新台账</span>
          </Button>
        </div>
      </div>

      {/* Main List */}
      <div className="flex-1 overflow-y-auto p-3.5 sm:p-6 space-y-3.5">
        {historyList.length === 0 ? (
          <div className="flex flex-col items-center justify-center p-12 text-center text-slate-400 dark:text-slate-500 bg-white dark:bg-slate-900 rounded-2xl border border-slate-200 dark:border-slate-800">
            <History className="h-10 w-10 text-slate-300 dark:text-slate-600 stroke-1 mb-2" />
            <p className="text-xs font-semibold text-slate-600 dark:text-slate-300">暂无历史执行台账</p>
            <p className="text-[11px] text-slate-400 dark:text-slate-500 mt-1">
              在「批量任务执行」中执行批次任务后，回执明细与对账数据将自动沉淀至此
            </p>
            {onSwitchToBatch && (
              <Button
                variant="outline"
                size="sm"
                onClick={onSwitchToBatch}
                className="mt-4 gap-1.5 text-xs text-blue-600 dark:text-blue-400 border-blue-200 dark:border-blue-800 bg-blue-50/50 dark:bg-blue-950/30 hover:bg-blue-100 cursor-pointer"
              >
                <Play className="h-3.5 w-3.5" />
                <span>立即前往批量任务执行</span>
              </Button>
            )}
          </div>
        ) : (
          <div className="grid grid-cols-1 gap-3">
            {historyList.map((item) => {
              const successRate =
                item.total > 0 ? Math.round((item.success / item.total) * 100) : 0

              return (
                <Card
                  key={item.task_id}
                  className="border-slate-200/90 dark:border-slate-800 dark:bg-slate-900 hover:border-blue-300 dark:hover:border-blue-700 transition-all shadow-2xs hover:shadow-xs group"
                >
                  <CardContent className="p-3.5 flex flex-col md:flex-row md:items-center gap-3.5 md:gap-4">
                    {/* Zone 1 (Left): Info (弹性自适应) */}
                    <div className="flex-1 min-w-0 flex items-center gap-3">
                      <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-blue-50 text-blue-600 border border-blue-100 dark:bg-blue-950/60 dark:text-blue-400 dark:border-blue-900/60 shadow-2xs">
                        <FileSpreadsheet className="h-5 w-5" />
                      </div>
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-2">
                          <span
                            className="text-sm font-bold text-slate-900 dark:text-slate-100 truncate"
                            title={item.api_name || item.api_id}
                          >
                            {item.api_name || item.api_id}
                          </span>
                          {(() => {
                            const svcType = item.service_type || (item.api_id.startsWith('live_') ? 'live' : 'im')
                            const svcTag = svcType === 'live' ? 'CSS' : (svcType === 'im' ? 'TIM' : svcType.toUpperCase())
                            const isLive = svcType === 'live'
                            const isIm = svcType === 'im'
                            const colorClass = isLive
                              ? 'bg-emerald-50 dark:bg-emerald-950/60 text-emerald-700 dark:text-emerald-400 border-emerald-200 dark:border-emerald-800'
                              : isIm
                              ? 'bg-blue-50 dark:bg-blue-950/60 text-blue-700 dark:text-blue-400 border-blue-200 dark:border-blue-800'
                              : 'bg-purple-50 dark:bg-purple-950/60 text-purple-700 dark:text-purple-400 border-purple-200 dark:border-purple-800'
                            return (
                              <Badge
                                variant="outline"
                                className={`font-mono text-[9px] px-1 py-0 shrink-0 ${colorClass}`}
                              >
                                {svcTag}
                              </Badge>
                            )
                          })()}
                          <Badge
                            variant={item.failed === 0 ? 'success' : 'warning'}
                            className="font-mono text-[10px] px-1.5 py-0 shrink-0"
                          >
                            {item.status} ({successRate}%)
                          </Badge>
                        </div>
                        <div className="flex flex-wrap items-center gap-2 text-xs text-slate-400 font-mono mt-1">
                          <span className="text-slate-600 dark:text-slate-400 font-bold">
                            ID: {item.task_id}
                          </span>
                          <span>·</span>
                          <span>{item.created_at}</span>
                          <span>·</span>
                          <span>耗时: {item.duration_sec}s</span>
                        </div>
                      </div>
                    </div>

                    {/* Zone 2 (Middle): Metrics (大屏固定宽度 w-56 居中并列，小屏横向平铺三等分) */}
                    <div className="w-full md:w-56 shrink-0 border-y md:border-y-0 md:border-x border-slate-100 dark:border-slate-800/80 py-2 md:py-0 px-3 grid grid-cols-3 gap-2 text-center font-mono text-xs bg-slate-50/60 dark:bg-slate-800/40 md:bg-transparent rounded-lg md:rounded-none">
                      <div>
                        <div className="text-slate-400 text-[10px]">总条数</div>
                        <div className="font-bold text-slate-700 dark:text-slate-200 text-sm">
                          {item.total}
                        </div>
                      </div>
                      <div>
                        <div className="text-slate-400 text-[10px]">成功</div>
                        <div className="font-bold text-emerald-600 dark:text-emerald-400 text-sm">
                          {item.success}
                        </div>
                      </div>
                      <div>
                        <div className="text-slate-400 text-[10px]">失败</div>
                        <div
                          className={`font-bold text-sm ${
                            item.failed > 0
                              ? 'text-rose-600 dark:text-rose-400'
                              : 'text-slate-400 dark:text-slate-500'
                          }`}
                        >
                          {item.failed}
                        </div>
                      </div>
                    </div>

                    {/* Zone 3 (Right): Actions (大屏固定宽度 w-[310px]，小屏全宽自适应，按钮恒定紧凑) */}
                    <div className="w-full md:w-[310px] shrink-0 flex flex-wrap sm:flex-nowrap items-center justify-end gap-1.5 sm:gap-2 pt-1 md:pt-0">
                      {/* 一键载入至 Runner */}
                      <Button
                        size="sm"
                        onClick={() => handleLoadIntoRunner(item)}
                        className="h-8 gap-1 px-2.5 text-xs bg-blue-600 hover:bg-blue-500 text-white shadow-2xs cursor-pointer font-medium flex-1 sm:flex-initial justify-center shrink-0"
                        title="将本次任务的目标接口与入参数据直接装载到批处理中心中重新执行"
                      >
                        <RotateCcw className="h-3.5 w-3.5" />
                        <span>载入至 Runner</span>
                      </Button>

                      {/* 查看明细 */}
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => fetchDetail(item.task_id)}
                        className="h-8 gap-1 px-2.5 text-xs text-slate-700 dark:text-slate-300 hover:text-blue-600 dark:hover:text-blue-400 cursor-pointer shrink-0"
                      >
                        <Eye className="h-3.5 w-3.5" />
                        <span>明细</span>
                      </Button>

                      {/* 统一导出 CSV 下拉菜单（恒定占用 1 个按钮位，支持全量与失败项） */}
                      <ExportDropdown
                        taskId={item.task_id}
                        total={item.total}
                        failed={item.failed}
                      />

                      {/* 删除单条 */}
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={(e) => handleDeleteTask(e, item.task_id)}
                        className="h-8 w-8 p-0 text-slate-400 hover:text-rose-600 dark:hover:text-rose-400 cursor-pointer shrink-0"
                        title="删除该条历史"
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                      </Button>
                    </div>
                  </CardContent>
                </Card>
              )
            })}
          </div>
        )}
      </div>

      {/* Task Details Modal (高保真明细弹窗) */}
      {selectedTask && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/50 dark:bg-slate-950/80 p-2 sm:p-4 backdrop-blur-xs">
          <div className="flex max-h-[92vh] sm:max-h-[88vh] w-full max-w-5xl flex-col rounded-xl sm:rounded-2xl border border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900 shadow-2xl overflow-hidden animate-in fade-in-50 zoom-in-95 duration-150">
            {/* Modal Header */}
            <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-100 dark:border-slate-800 p-3 sm:p-4 px-4 sm:px-6 bg-slate-50/60 dark:bg-slate-950/50">
              <div>
                <div className="flex items-center gap-2">
                  <h3 className="text-sm font-bold text-slate-900 dark:text-slate-100">
                    任务回执台账: {selectedTask.api_name || selectedTask.api_id}
                  </h3>
                  <Badge variant="outline" className="font-mono text-[10px] text-slate-500">
                    ID: {selectedTask.task_id}
                  </Badge>
                </div>
                <p className="font-mono text-xs text-slate-400 mt-0.5">
                  创建时间: {selectedTask.created_at} · 耗时: {selectedTask.duration_sec}s · 总记录:{' '}
                  {selectedTask.total} 条 (成功: {selectedTask.success}, 失败: {selectedTask.failed})
                </p>
              </div>

              <div className="flex items-center gap-1.5 sm:gap-2 flex-wrap">
                {/* 载入到 Runner */}
                <Button
                  size="sm"
                  onClick={() => handleLoadIntoRunner(selectedTask)}
                  className="h-8 gap-1 text-xs bg-blue-600 hover:bg-blue-500 text-white cursor-pointer"
                  title="将本批次数据与接口直接装载到 Runner 中"
                >
                  <RotateCcw className="h-3.5 w-3.5" />
                  <span>
                    <span className="hidden sm:inline">载入至 Runner</span>
                    <span className="sm:hidden">重跑</span>
                  </span>
                </Button>

                {/* 导出全量 CSV */}
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => handleExport(selectedTask.task_id, false)}
                  className="h-8 gap-1 text-xs text-slate-700 dark:text-slate-300 cursor-pointer"
                >
                  <Download className="h-3.5 w-3.5" />
                  <span>
                    <span className="hidden sm:inline">导出全部 CSV</span>
                    <span className="sm:hidden">全量 CSV</span>
                  </span>
                </Button>

                {/* 导出失败项 CSV */}
                {selectedTask.failed > 0 && (
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => handleExport(selectedTask.task_id, true)}
                    className="h-8 gap-1 text-xs text-rose-700 dark:text-rose-300 border-rose-300 dark:border-rose-800 bg-rose-50/70 dark:bg-rose-950/30 hover:bg-rose-100 dark:hover:bg-rose-900/50 cursor-pointer"
                  >
                    <Download className="h-3.5 w-3.5" />
                    <span>
                      <span className="hidden sm:inline">导出失败项 ({selectedTask.failed})</span>
                      <span className="sm:hidden">失败项 ({selectedTask.failed})</span>
                    </span>
                  </Button>
                )}

                <button
                  type="button"
                  onClick={clearSelected}
                  className="rounded-lg p-1 text-slate-400 hover:bg-slate-100 dark:hover:bg-slate-800 hover:text-slate-600 dark:hover:text-slate-300 cursor-pointer ml-1"
                >
                  <X className="h-5 w-5" />
                </button>
              </div>
            </div>

            {/* Filter Tabs & Search Bar */}
            <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-100 dark:border-slate-800 px-6 py-2.5 bg-white dark:bg-slate-900">
              {/* Status Filter Tabs */}
              <div className="flex items-center gap-1.5">
                <button
                  type="button"
                  onClick={() => setFilterStatus('ALL')}
                  className={`px-2.5 py-1 text-xs rounded-lg font-medium transition-colors cursor-pointer ${
                    filterStatus === 'ALL'
                      ? 'bg-slate-900 text-white dark:bg-white dark:text-slate-900 font-bold'
                      : 'text-slate-600 hover:bg-slate-100 dark:text-slate-400 dark:hover:bg-slate-800'
                  }`}
                >
                  全部 ({selectedTask.total})
                </button>
                <button
                  type="button"
                  onClick={() => setFilterStatus('FAILED')}
                  className={`px-2.5 py-1 text-xs rounded-lg font-medium transition-colors cursor-pointer flex items-center gap-1 ${
                    filterStatus === 'FAILED'
                      ? 'bg-rose-600 text-white font-bold'
                      : 'text-rose-600 hover:bg-rose-50 dark:text-rose-400 dark:hover:bg-rose-950/50'
                  }`}
                >
                  <span>仅失败</span>
                  <span className="font-mono text-[10px]">({selectedTask.failed})</span>
                </button>
                <button
                  type="button"
                  onClick={() => setFilterStatus('SUCCESS')}
                  className={`px-2.5 py-1 text-xs rounded-lg font-medium transition-colors cursor-pointer flex items-center gap-1 ${
                    filterStatus === 'SUCCESS'
                      ? 'bg-emerald-600 text-white font-bold'
                      : 'text-emerald-600 hover:bg-emerald-50 dark:text-emerald-400 dark:hover:bg-emerald-950/50'
                  }`}
                >
                  <span>仅成功</span>
                  <span className="font-mono text-[10px]">({selectedTask.success})</span>
                </button>
              </div>

              {/* Keyword Search */}
              <div className="relative w-64">
                <Search className="h-3.5 w-3.5 text-slate-400 absolute left-2.5 top-2.5 pointer-events-none" />
                <input
                  type="text"
                  value={searchTerm}
                  onChange={(e) => setSearchTerm(e.target.value)}
                  placeholder="搜索任何字段、错误码或内容..."
                  className="h-8 w-full rounded-xl border border-slate-200 bg-slate-50/60 pl-8 pr-3 text-xs text-slate-800 placeholder:text-slate-400 focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-slate-800 dark:bg-slate-800 dark:text-slate-200"
                />
              </div>
            </div>

            {/* Modal Body: Table of results */}
            <div className="flex-1 overflow-y-auto p-6">
              {isDetailLoading ? (
                <div className="flex h-40 items-center justify-center text-xs text-slate-400">
                  <RefreshCw className="h-4 w-4 animate-spin mr-2" />
                  <span>正在从存储区拉取回执明细...</span>
                </div>
              ) : filteredResults.length > 0 ? (
                <div className="rounded-xl border border-slate-200 dark:border-slate-800 overflow-x-auto shadow-2xs">
                  <table className="w-full text-left text-xs border-collapse">
                    <thead>
                      <tr className="bg-slate-50 dark:bg-slate-800/80 border-b border-slate-200 dark:border-slate-700 text-slate-600 dark:text-slate-300 font-semibold font-mono text-[11px]">
                        <th className="py-2 px-3 w-12">#</th>
                        <th className="py-2 px-3 w-16">状态</th>
                        <th className="py-2 px-3 w-20">错误码</th>
                        <th className="py-2 px-3 w-20">耗时</th>

                        {/* 动态业务参数列（自适应 166 个接口的所有不同字段） */}
                        {dynamicParamColumns.map((col) => (
                          <th key={col} className="py-2 px-3">
                            {col}
                          </th>
                        ))}

                        <th className="py-2 px-3 min-w-[200px]">腾讯云返回详情 / 错误说明</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-100 dark:divide-slate-800 font-mono text-[11px]">
                      {filteredResults.map((row: any, idx: number) => {
                        // 精准字段对齐（修复下划线映射）
                        const status = row._status || row.status || 'OK'
                        const errCode =
                          row._err_code !== undefined
                            ? row._err_code
                            : row.err_code !== undefined
                            ? row.err_code
                            : 0
                        const costMs = row._cost_ms !== undefined ? row._cost_ms : row.cost_ms
                        const errInfo = row._err_info || row.err_info || '-'
                        const isOk = status === 'OK' && Number(errCode) === 0

                        return (
                          <tr
                            key={idx}
                            className="hover:bg-slate-50/70 dark:hover:bg-slate-800/60 transition-colors"
                          >
                            <td className="py-2 px-3 text-slate-400 dark:text-slate-500">{idx + 1}</td>
                            <td className="py-2 px-3">
                              <Badge
                                variant={isOk ? 'success' : 'destructive'}
                                className="font-mono text-[10px] px-1.5 py-0"
                              >
                                {isOk ? 'OK' : 'FAIL'}
                              </Badge>
                            </td>
                            <td
                              className={`py-2 px-3 font-bold ${
                                isOk
                                  ? 'text-slate-700 dark:text-slate-200'
                                  : 'text-rose-600 dark:text-rose-400'
                              }`}
                            >
                              {errCode}
                            </td>
                            <td className="py-2 px-3 text-slate-500 dark:text-slate-400">
                              {costMs !== undefined ? `${costMs}ms` : '-'}
                            </td>

                            {/* 动态入参值 */}
                            {dynamicParamColumns.map((col) => (
                              <td
                                key={col}
                                className="py-2 px-3 text-slate-800 dark:text-slate-200 truncate max-w-xs"
                                title={String(row[col] ?? '')}
                              >
                                {String(row[col] ?? '') || '-'}
                              </td>
                            ))}

                            <td
                              className={`py-2 px-3 break-all ${
                                isOk
                                  ? 'text-slate-500 dark:text-slate-400'
                                  : 'text-rose-600 dark:text-rose-400 font-medium'
                              }`}
                            >
                              {errInfo}
                            </td>
                          </tr>
                        )
                      })}
                    </tbody>
                  </table>
                </div>
              ) : (
                <div className="p-12 text-center text-xs text-slate-400 dark:text-slate-500">
                  没有匹配的执行记录明细
                </div>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
