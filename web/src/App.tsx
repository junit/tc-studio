import React, { useState, useEffect } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import {
  ChevronLeft,
  ChevronRight,
  CheckCircle2,
  AlertCircle,
  X,
  Terminal,
  BookOpen,
  Layers,
  Settings,
  Bot,
} from 'lucide-react'
import { Header } from '@/components/common/Header'
import { ApiTreePanel } from '@/components/panels/ApiTreePanel'
import { ApiWorkbenchPanel } from '@/components/panels/ApiWorkbenchPanel'
import { BatchCenter } from '@/components/panels/BatchCenter'
import { SettingsPanel } from '@/components/panels/SettingsPanel'
import { DocsCenterPanel } from '@/components/panels/DocsCenterPanel'
import { AgentPanel } from '@/components/panels/AgentPanel'
import { useApiStore } from '@/stores/apiStore'
import { readTcStorage, writeTcStorage } from '@/lib/storage'

export type MainNavTab = 'sandbox' | 'docs' | 'batch' | 'agent' | 'settings'

const VALID_TABS: MainNavTab[] = ['sandbox', 'docs', 'batch', 'agent', 'settings']
const NAV_STORAGE_KEY = 'active_main_nav_tab'

function getInitialNavTab(): MainNavTab {
  if (typeof window === 'undefined') return 'sandbox'

  // 1. 优先读取 URL hash (如 #docs, #batch, #agent, #settings, #sandbox)
  const hash = window.location.hash.replace(/^#/, '').toLowerCase()
  if (VALID_TABS.includes(hash as MainNavTab)) {
    return hash as MainNavTab
  }

  // 2. 读取持久化缓存
  try {
    const saved = readTcStorage(localStorage, NAV_STORAGE_KEY)
    if (saved && VALID_TABS.includes(saved as MainNavTab)) {
      return saved as MainNavTab
    }
  } catch (e) {
    console.warn('Failed to read nav tab from localStorage', e)
  }

  return 'sandbox'
}

export function App() {
  const [activeTab, setActiveTab] = useState<MainNavTab>(getInitialNavTab)
  const [isMobile, setIsMobile] = useState<boolean>(() =>
    typeof window !== 'undefined' ? window.innerWidth < 1024 : false
  )

  const {
    fetchApis,
    isSidebarOpen,
    toggleSidebar,
    sidebarWidth,
    setSidebarWidth,
    toast,
    dismissToast,
  } = useApiStore()

  // 监听窗口尺寸变化，自适应移动端与桌面端
  useEffect(() => {
    const handleResize = () => {
      setIsMobile(window.innerWidth < 1024)
    }
    handleResize()
    window.addEventListener('resize', handleResize)
    return () => window.removeEventListener('resize', handleResize)
  }, [])

  // 快捷键 Cmd+B / Ctrl+B 切换侧边栏收缩与展开
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'b') {
        e.preventDefault()
        toggleSidebar()
      }
    }
    window.addEventListener('keydown', handleKeyDown)
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [toggleSidebar])

  // 持久化当前激活的顶部主导航 Tab 并静默同步 URL hash
  useEffect(() => {
    try {
      writeTcStorage(localStorage, NAV_STORAGE_KEY, activeTab)
      if (typeof window !== 'undefined' && window.location.hash.replace(/^#/, '') !== activeTab) {
        window.history.replaceState(null, '', `#${activeTab}`)
      }
    } catch (e) {
      console.warn('Failed to persist nav tab to localStorage', e)
    }
  }, [activeTab])

  // 监听浏览器 URL hash 变动（如前进/后退或外部跳转）
  useEffect(() => {
    const handleHashChange = () => {
      const hash = window.location.hash.replace(/^#/, '').toLowerCase()
      if (VALID_TABS.includes(hash as MainNavTab)) {
        setActiveTab(hash as MainNavTab)
      }
    }
    window.addEventListener('hashchange', handleHashChange)
    return () => window.removeEventListener('hashchange', handleHashChange)
  }, [])

  useEffect(() => {
    fetchApis()
  }, [fetchApis])

  // 鼠标拖拽微调固定宽度（双击快速重置为 320px）
  const handleMouseDown = (e: React.MouseEvent) => {
    e.preventDefault()
    const startX = e.clientX
    const startWidth = sidebarWidth

    const onMouseMove = (moveEvent: MouseEvent) => {
      const delta = moveEvent.clientX - startX
      setSidebarWidth(startWidth + delta)
    }

    const onMouseUp = () => {
      window.removeEventListener('mousemove', onMouseMove)
      window.removeEventListener('mouseup', onMouseUp)
    }

    window.addEventListener('mousemove', onMouseMove)
    window.addEventListener('mouseup', onMouseUp)
  }

  const handleDoubleClickSplitter = () => {
    setSidebarWidth(320)
  }

  return (
    <div className="flex h-screen w-screen flex-col overflow-hidden bg-slate-50 dark:bg-slate-950 font-sans text-slate-900 dark:text-slate-100">
      {/* 顶部 Header（纯净无多余收缩按钮） */}
      <Header activeTab={activeTab} setActiveTab={setActiveTab} />

      {/* 主界面容器 */}
      <main className="flex-1 overflow-hidden relative pb-14 lg:pb-0">
        {activeTab === 'sandbox' && (
          <div className="flex h-full w-full overflow-hidden relative">
            {/* 移动端抽屉遮罩 (Backdrop) */}
            {isMobile && isSidebarOpen && (
              <div
                onClick={toggleSidebar}
                className="fixed inset-0 bg-slate-900/60 backdrop-blur-xs z-40 lg:hidden animate-in fade-in-50 duration-200"
              />
            )}

            {/* 树型导航栏：大屏为固定/可拖拽侧边栏，移动端为轻巧浮层抽屉 */}
            <motion.aside
              initial={false}
              animate={{
                width: isMobile
                  ? (isSidebarOpen ? Math.min(340, typeof window !== 'undefined' ? window.innerWidth * 0.85 : 320) : 0)
                  : (isSidebarOpen ? sidebarWidth : 0),
                opacity: isSidebarOpen ? 1 : 0,
              }}
              transition={{ duration: 0.2, ease: 'easeInOut' }}
              className={`h-full shrink-0 overflow-hidden bg-white dark:bg-slate-900 ${
                isMobile
                  ? 'fixed inset-y-0 left-0 z-50 shadow-2xl border-r border-slate-200 dark:border-slate-800'
                  : 'relative z-20'
              }`}
            >
              <div
                style={{
                  width: isMobile
                    ? Math.min(340, typeof window !== 'undefined' ? window.innerWidth * 0.85 : 320)
                    : sidebarWidth,
                }}
                className="h-full"
              >
                <ApiTreePanel
                  isMobile={isMobile}
                  onNavigateToBatch={() => {
                    setActiveTab('batch')
                    if (isMobile && isSidebarOpen) toggleSidebar()
                  }}
                  onCloseSidebar={() => {
                    if (isSidebarOpen) toggleSidebar()
                  }}
                />
              </div>
            </motion.aside>

            {/* 大屏模式下的栏目分隔线与拖拽手柄 */}
            {isSidebarOpen ? (
              <div
                onMouseDown={handleMouseDown}
                onDoubleClick={handleDoubleClickSplitter}
                title="左右拖拽微调宽度，双击复位 320px"
                className="hidden lg:flex group relative w-1 h-full cursor-col-resize items-center justify-center bg-slate-200/90 dark:bg-slate-800 hover:bg-blue-500/40 active:bg-blue-600 transition-colors z-30"
              >
                {/* 居中一体化清晰收起手柄（16px宽 x 56px高） */}
                <button
                  type="button"
                  onClick={(e) => {
                    e.stopPropagation()
                    toggleSidebar()
                  }}
                  onMouseDown={(e) => e.stopPropagation()}
                  title="点击收起侧边栏 (快捷键 ⌘B)"
                  className="absolute top-1/2 -translate-y-1/2 -left-2 flex h-14 w-4.5 items-center justify-center rounded-full border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 shadow-xs hover:shadow-md hover:border-blue-400 dark:hover:border-blue-500 hover:bg-blue-50/60 dark:hover:bg-slate-700 text-slate-400 hover:text-blue-600 dark:hover:text-blue-400 hover:scale-105 active:scale-95 transition-all cursor-pointer z-40 group"
                >
                  <ChevronLeft className="h-3.5 w-3.5 group-hover:-translate-x-0.5 transition-transform" />
                </button>
              </div>
            ) : (
              <>
                {/* 桌面大屏收起状态：居中停靠在屏幕最左侧的竖向展开标签手柄 */}
                <button
                  type="button"
                  onClick={toggleSidebar}
                  title="点击展开 API 导航栏 (快捷键 ⌘B)"
                  className="hidden lg:flex absolute left-0 top-1/2 -translate-y-1/2 flex-col items-center justify-center gap-1.5 py-3 px-1.5 rounded-r-xl border-y border-r border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 shadow-md hover:shadow-lg hover:border-blue-400 dark:hover:border-blue-500 hover:bg-blue-50/30 dark:hover:bg-slate-700 text-slate-600 dark:text-slate-300 hover:text-blue-600 dark:hover:text-blue-400 hover:pl-2 transition-all cursor-pointer z-30 group"
                >
                  <ChevronRight className="h-3.5 w-3.5 text-blue-600 dark:text-blue-400 group-hover:translate-x-0.5 transition-transform" />
                  <span className="text-[10px] font-bold tracking-widest text-slate-600 dark:text-slate-300 group-hover:text-blue-600 dark:group-hover:text-blue-400 [writing-mode:vertical-lr] select-none py-1">
                    API 导航
                  </span>
                </button>

                {/* 移动端/小屏收起状态：悬浮于左下角的轻盈 Pill 胶囊按钮（不遮挡中部表单） */}
                <button
                  type="button"
                  onClick={toggleSidebar}
                  title="点击展开 API 导航抽屉"
                  className="fixed bottom-18 left-3 z-30 flex lg:hidden items-center gap-1.5 py-1.5 px-3 rounded-full bg-blue-600 hover:bg-blue-500 text-white shadow-xl shadow-blue-600/30 active:scale-95 transition-all cursor-pointer"
                >
                  <Layers className="h-3.5 w-3.5" />
                  <span className="text-xs font-semibold">API 导航</span>
                </button>
              </>
            )}

            {/* 右侧互动调试沙箱工作台 */}
            <section className="flex-1 h-full min-w-0 overflow-hidden relative">
              <ApiWorkbenchPanel onSwitchToBatch={() => setActiveTab('batch')} />
            </section>
          </div>
        )}

        {activeTab === 'docs' && (
          <DocsCenterPanel onNavigateToSandbox={() => setActiveTab('sandbox')} />
        )}

        {activeTab === 'batch' && <BatchCenter />}

        {activeTab === 'agent' && <AgentPanel />}

        {activeTab === 'settings' && <SettingsPanel />}
      </main>

      {/* 移动端/平板专属：底部快捷原生 Tab 导航条 (LG 屏幕 1024px 以上自动隐藏) */}
      <nav className="flex lg:hidden fixed bottom-0 left-0 right-0 z-40 bg-white/95 dark:bg-slate-900/95 backdrop-blur-md border-t border-slate-200/80 dark:border-slate-800 items-center justify-around py-1.5 px-2 shadow-lg">
        <button
          type="button"
          onClick={() => setActiveTab('sandbox')}
          className={`flex flex-col items-center gap-0.5 py-1 px-2.5 rounded-lg text-[10px] font-semibold transition-colors cursor-pointer ${
            activeTab === 'sandbox'
              ? 'text-blue-600 dark:text-blue-400 font-bold'
              : 'text-slate-500 dark:text-slate-400 hover:text-slate-900 dark:hover:text-slate-200'
          }`}
        >
          <Terminal className="h-4 w-4" />
          <span>接口调试</span>
        </button>
        <button
          type="button"
          onClick={() => setActiveTab('docs')}
          className={`flex flex-col items-center gap-0.5 py-1 px-2.5 rounded-lg text-[10px] font-semibold transition-colors cursor-pointer ${
            activeTab === 'docs'
              ? 'text-blue-600 dark:text-blue-400 font-bold'
              : 'text-slate-500 dark:text-slate-400 hover:text-slate-900 dark:hover:text-slate-200'
          }`}
        >
          <BookOpen className="h-4 w-4" />
          <span>文档中心</span>
        </button>
        <button
          type="button"
          onClick={() => setActiveTab('batch')}
          className={`flex flex-col items-center gap-0.5 py-1 px-2.5 rounded-lg text-[10px] font-semibold transition-colors cursor-pointer ${
            activeTab === 'batch'
              ? 'text-blue-600 dark:text-blue-400 font-bold'
              : 'text-slate-500 dark:text-slate-400 hover:text-slate-900 dark:hover:text-slate-200'
          }`}
        >
          <Layers className="h-4 w-4" />
          <span>批量调用</span>
        </button>
        <button
          type="button"
          onClick={() => setActiveTab('agent')}
          className={`flex flex-col items-center gap-0.5 py-1 px-2.5 rounded-lg text-[10px] font-semibold transition-colors cursor-pointer ${
            activeTab === 'agent'
              ? 'text-blue-600 dark:text-blue-400 font-bold'
              : 'text-slate-500 dark:text-slate-400 hover:text-slate-900 dark:hover:text-slate-200'
          }`}
        >
          <Bot className="h-4 w-4" />
          <span>AI 智能体</span>
        </button>
        <button
          type="button"
          onClick={() => setActiveTab('settings')}
          className={`flex flex-col items-center gap-0.5 py-1 px-2.5 rounded-lg text-[10px] font-semibold transition-colors cursor-pointer ${
            activeTab === 'settings'
              ? 'text-blue-600 dark:text-blue-400 font-bold'
              : 'text-slate-500 dark:text-slate-400 hover:text-slate-900 dark:hover:text-slate-200'
          }`}
        >
          <Settings className="h-4 w-4" />
          <span>环境配置</span>
        </button>
      </nav>

      {/* 全局交互 Toast 提示 (带一键操作) */}
      <AnimatePresence>
        {toast && (
          <motion.div
            initial={{ opacity: 0, y: 30, scale: 0.9 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: 20, scale: 0.9 }}
            transition={{ duration: 0.2 }}
            className="fixed bottom-16 md:bottom-6 right-3 md:right-6 z-50 flex items-center gap-3 px-4 py-3 rounded-2xl border border-slate-700/80 bg-slate-900/95 text-white shadow-2xl backdrop-blur-md"
          >
            {toast.type === 'error' ? (
              <AlertCircle className="h-4 w-4 text-rose-400 shrink-0" />
            ) : (
              <CheckCircle2 className="h-4 w-4 text-emerald-400 shrink-0" />
            )}
            <span className="text-xs font-medium text-slate-100">{toast.message}</span>
            {toast.actionLabel && (
              <button
                type="button"
                onClick={() => {
                  toast.onAction?.()
                  dismissToast()
                }}
                className="px-2.5 py-1 text-xs font-semibold rounded-lg bg-blue-600 hover:bg-blue-500 text-white transition-colors cursor-pointer shrink-0 ml-1"
              >
                {toast.actionLabel}
              </button>
            )}
            <button
              type="button"
              onClick={dismissToast}
              className="text-slate-400 hover:text-white p-1 ml-0.5 cursor-pointer rounded-lg hover:bg-slate-800"
            >
              <X className="h-3.5 w-3.5" />
            </button>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}

export default App
