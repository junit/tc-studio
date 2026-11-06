import React, { useState, useEffect, useRef, useMemo, useCallback } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import {
  Bot,
  Sparkles,
  Send,
  Square,
  Plus,
  Trash2,
  Edit3,
  Brain,
  Zap,
  Settings,
  AtSign,
  RefreshCw,
  Copy,
  Check,
  Shield,
  Search,
  ExternalLink,
  History,
  Key,
  Server,
  ArrowRight,
  FileText,
  Lock,
  ArrowDown,
  CheckCircle2,
  Loader2,
  Paperclip,
  FileSpreadsheet,
  Image as ImageIcon,
  X,
  Eye,
  Download,
  FolderArchive,
  Archive,
  RotateCcw,
  BookOpen,
  Share2,
  Globe,
  Menu,
} from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { useApiStore } from '@/stores/apiStore'
import { MarkdownView } from '@/components/common/MarkdownView'
import { AuthenticatedImage } from '@/components/common/AuthenticatedImage'
import { downloadWithAuth, openWithAuth } from '@/lib/auth'
import {
  AgentExecutionRun,
  buildAgentRunFromMessage,
} from './agent/AgentMessageCards'
import { AgentTopologyViewer } from '@/components/agent/AgentTopologyViewer'
import type { OrchestrationEvent } from '@/components/agent/AgentTopologyViewer'
import { ConsentGateCard, ApprovalData } from './agent/ConsentGateCard'
import { AgentPromptTab } from './agent/drawer/AgentPromptTab'
import { AgentGuidelinesTab } from './agent/drawer/AgentGuidelinesTab'
import { AgentSkillsTab } from './agent/drawer/AgentSkillsTab'
import { AgentMemoryTab } from './agent/drawer/AgentMemoryTab'
import { AgentFilesTab as FilesTab } from './agent/drawer/AgentFilesTab'
import { AgentKnowledgeTab } from './agent/drawer/AgentKnowledgeTab'
import type { SkillItem } from './agent/drawer/types'
import {
  BotSummary,
  BotFileItem,
  KnowledgeDoc,
  LLMConfigData,
} from './agent/drawer/types'


interface AttachmentData {
  id: string
  name: string
  saved_name?: string
  size: number
  size_formatted?: string
  type: string
  category?: string
  path: string
  url?: string
  data_url?: string
  summary?: string
  preview?: string
  meta?: Record<string, unknown>
}


interface MessageMetrics {
  elapsed_seconds?: number
  input_tokens?: number
  output_tokens?: number
  total_tokens?: number
  code_executions?: number
  model?: string
}

interface ChatMetadata {
  orchestrations?: OrchestrationEvent[]
  metrics?: MessageMetrics
  [key: string]: unknown
}
interface ChatMessage {
  id: string
  role: 'user' | 'assistant'
  content: string
  attachments?: AttachmentData[]
  thought?: string
  codeBlocks?: Array<{ step: number; code: string; status?: 'running' | 'success' | 'error' }>
  executionLogs?: Array<{ step: number; output: string; status?: 'running' | 'success' | 'error' }>
  pendingApproval?: ApprovalData
  toolCalls?: Array<{ step: number; output: string }>
  orchestrations?: any[]
  metadata?: ChatMetadata
  isStreaming?: boolean
  streamStatus?: 'thinking' | 'coding' | 'executing' | 'answering' | 'done' | 'error' | 'waiting_approval'
  currentStep?: number
  startTime?: number
  elapsedSeconds?: number
  timestamp?: string
}

interface ActiveRunSummary {
  run_id: string
  bot_id: string
  status: 'running' | 'done' | 'error' | 'stopped'
  stream_status?: 'thinking' | 'coding' | 'executing' | 'answering' | 'done' | 'error' | 'waiting_approval'
  start_time?: number
  elapsed_seconds?: number
  current_step?: number
  user_message?: string
  pending_approval?: ApprovalData
}


const AVATAR_PRESETS = ['🤖', '⚡', '🛡️', '🔍', '📊', '🚀', '💬', '👑', '🛠️', '🎯', '💡', '🐧', '🐱', '🦊', '🌐']

function formatChatTime(timestamp?: string): string {
  if (!timestamp) return ''
  try {
    const date = new Date(timestamp)
    if (isNaN(date.getTime())) return ''
    const now = new Date()
    const isToday =
      date.getDate() === now.getDate() &&
      date.getMonth() === now.getMonth() &&
      date.getFullYear() === now.getFullYear()
    if (isToday) {
      return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false })
    }
    const yesterday = new Date(now)
    yesterday.setDate(yesterday.getDate() - 1)
    const isYesterday =
      date.getDate() === yesterday.getDate() &&
      date.getMonth() === yesterday.getMonth() &&
      date.getFullYear() === yesterday.getFullYear()
    if (isYesterday) {
      return '昨天'
    }
    const isThisYear = date.getFullYear() === now.getFullYear()
    if (isThisYear) {
      return `${date.getMonth() + 1}/${date.getDate()}`
    }
    return `${date.getFullYear()}/${date.getMonth() + 1}/${date.getDate()}`
  } catch {
    return ''
  }
}

function getAttachmentIcon(category?: string, type?: string) {
  if (category === 'table' || type === 'xlsx' || type === 'csv' || type === 'xls') {
    return <FileSpreadsheet className="h-4 w-4 text-emerald-500" />
  }
  if (category === 'image') {
    return <ImageIcon className="h-4 w-4 text-purple-500" />
  }
  return <FileText className="h-4 w-4 text-blue-500" />
}

const BOT_AVATAR_PRESETS = [
  '🤖', '🛡️', '⚡', '🔍', '📊', '🛠️', '🎯', '💡',
  '🚀', '👑', '🧙', '🧭', '💬', '🎧', '🦾', '🧪',
]

const inferAvatarFromText = (text: string): string => {
  const t = text.toLowerCase()
  if (/风控|安全|拦截|黑名单|防护|违规|审核|security|risk/.test(t)) return '🛡️'
  if (/巡检|排查|监控|检测|诊断|健康|inspector|monitor/.test(t)) return '🔍'
  if (/群|社群|讨论组|成员|group/.test(t)) return '👥'
  if (/消息|聊天|对话|通知|推送|广播|撤回|msg|chat/.test(t)) return '💬'
  if (/数据|分析|统计|报表|对账|活跃|data|stats|metrics/.test(t)) return '📊'
  if (/运维|压测|性能|系统|高可用|ops|perf/.test(t)) return '⚡'
  if (/测试|验证|自动化|回归|test|qa/.test(t)) return '🧪'
  if (/客服|答疑|服务|接待|service|support/.test(t)) return '🎧'
  if (/文档|知识|帮助|规范|doc|wiki/.test(t)) return '📚'
  return '🤖'
}

export const AgentPanel: React.FC = () => {
  const showToast = useApiStore((state) => state.showToast)
  const apis = useApiStore((state) => state.apis)
  const activeServiceId = useApiStore((state) => state.activeServiceId)
  const totalApisCount = apis.length || 427

  // BOT 列表与当前选中
  const [bots, setBots] = useState<BotSummary[]>([])
  const [activeBotId, setActiveBotId] = useState<string>('default_assistant')
  const [activeBot, setActiveBot] = useState<any>(null)

  // 智能体业务域筛选（全部 / 当前业务专属 / 通用）
  const [botDomainFilter, setBotDomainFilter] = useState<'all' | 'current' | 'general'>('all')

  const getBotDomain = (bot: BotSummary): 'live' | 'im' | 'general' => {
    if (bot.id === 'default_assistant' || (bot as any).service_type === 'all' || (bot as any).service_type === 'general') {
      return 'general'
    }
    const svc = (bot as any).service_type
    if (svc === 'live' || svc === 'css') return 'live'
    if (svc === 'im' || svc === 'tim') return 'im'

    const target = (bot.id + ' ' + bot.name + ' ' + (bot.description || '')).toLowerCase()
    const hasLive = target.includes('live') || target.includes('css') || target.includes('直播')
    const hasIm = target.includes('im') || target.includes('tim') || target.includes('单聊') || target.includes('群聊')

    if (hasLive && !hasIm) return 'live'
    if (hasIm && !hasLive) return 'im'
    return 'general'
  }

  const filteredBots = useMemo(() => {
    return bots.filter((b) => {
      if (botDomainFilter === 'all') return true
      const domain = getBotDomain(b)
      if (botDomainFilter === 'current') {
        const curSvc = activeServiceId || 'im'
        return domain === curSvc || b.id === 'default_assistant'
      }
      if (botDomainFilter === 'general') {
        return domain === 'general'
      }
      return true
    })
  }, [bots, botDomainFilter, activeServiceId])

  // 会话状态
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [hasMoreHistory, setHasMoreHistory] = useState<boolean>(false)
  const [isLoadingMoreHistory, setIsLoadingMoreHistory] = useState<boolean>(false)
  const [totalHistoryCount, setTotalHistoryCount] = useState<number>(0)
  const [inputMessage, setInputMessage] = useState<string>('')
  const [isGenerating, setIsGenerating] = useState<boolean>(false)
  const abortControllerRef = useRef<AbortController | null>(null)
  const activeBotIdRef = useRef(activeBotId)
  const botRequestEpochRef = useRef(0)
  const isCurrentBotRequest = (botId: string, epoch: number) =>
    activeBotIdRef.current === botId && botRequestEpochRef.current === epoch
  const messagesEndRef = useRef<HTMLDivElement>(null)
  const messagesContainerRef = useRef<HTMLDivElement>(null)
  const [showScrollBottom, setShowScrollBottom] = useState<boolean>(false)

  // 附件上传状态
  const [attachedFiles, setAttachedFiles] = useState<AttachmentData[]>([])
  // 本地预览 Blob URL 登记：移除附件/卸载组件时统一 revoke，避免内存泄漏
  const localPreviewUrlsRef = useRef<Set<string>>(new Set())
  const revokeLocalPreviewUrl = (url?: string) => {
    if (url && url.startsWith('blob:') && localPreviewUrlsRef.current.delete(url)) {
      URL.revokeObjectURL(url)
    }
  }
  const revokeDetachedPreviewUrls = (nextMessages: Array<{ attachments?: AttachmentData[] }>) => {
    const retained = new Set<string>()
    for (const message of nextMessages) {
      for (const attachment of message.attachments || []) {
        if (attachment.data_url?.startsWith('blob:')) retained.add(attachment.data_url)
      }
    }
    for (const url of [...localPreviewUrlsRef.current]) {
      if (!retained.has(url)) revokeLocalPreviewUrl(url)
    }
  }
  useEffect(() => {
    const tracked = localPreviewUrlsRef.current
    return () => {
      tracked.forEach((url) => URL.revokeObjectURL(url))
      tracked.clear()
    }
  }, [])
  const [isUploadingFile, setIsUploadingFile] = useState<boolean>(false)
  const [isDraggingOver, setIsDraggingOver] = useState<boolean>(false)
  const fileInputRef = useRef<HTMLInputElement>(null)

  // 全局所有 BOT 的后台运行状态（映射 bot_id -> ActiveRunSummary）
  const [activeRunsMap, setActiveRunsMap] = useState<Record<string, ActiveRunSummary>>({})

  // 查询全局正在运行的 BOT 任务列表
  const fetchActiveRuns = useCallback(async () => {
    try {
      const res = await fetch('/api/agent/active-runs')
      if (res.ok) {
        const data = await res.json()
        if (data.active_runs) {
          setActiveRunsMap(data.active_runs)
        }
      }
    } catch {
      // 忽略短暂网络闪断
    }
  }, [])

  // 定时与自适应轮询后台运行任务（有活跃任务时以 1.8s 高频刷新，无活跃任务时以 3.5s 低频轮询）
  useEffect(() => {
    fetchActiveRuns()
    const hasActive = Object.keys(activeRunsMap).length > 0 || isGenerating
    const timer = setInterval(() => {
      fetchActiveRuns()
    }, hasActive ? 1800 : 3500)
    return () => clearInterval(timer)
  }, [fetchActiveRuns, isGenerating, Object.keys(activeRunsMap).length])

  // 批量上传文件核心方法
  const uploadFiles = async (files: FileList | File[]) => {
    if (!files || files.length === 0) return
    setIsUploadingFile(true)
    try {
      for (const file of Array.from(files)) {
        // 对图片即刻生成本地零开销 Blob URL 用于即时预览
        let localPreviewUrl: string | undefined = undefined
        if (file.type.startsWith('image/')) {
          try {
            localPreviewUrl = URL.createObjectURL(file)
            localPreviewUrlsRef.current.add(localPreviewUrl)
          } catch (error) {
            console.warn('Failed to create local preview URL', error)
          }
        }

        const formData = new FormData()
        formData.append('file', file)
        const res = await fetch(`/api/agent/bots/${activeBotId}/upload`, {
          method: 'POST',
          body: formData,
        })
        if (res.ok) {
          const data = await res.json()
          if (data.file) {
            setAttachedFiles((prev) => [
              ...prev,
              {
                ...data.file,
                data_url: localPreviewUrl || data.file.data_url,
              },
            ])
            showToast({ type: 'success', message: `已成功解析附件「${file.name}」` }, 2500)
          } else {
            revokeLocalPreviewUrl(localPreviewUrl)
          }
        } else {
          revokeLocalPreviewUrl(localPreviewUrl)
          showToast({ type: 'error', message: `文件「${file.name}」上传失败` }, 3500)
        }
      }
    } catch (err: any) {
      showToast({ type: 'error', message: '附件上传异常: ' + (err.message || '网络连接失败') })
    } finally {
      setIsUploadingFile(false)
      if (fileInputRef.current) fileInputRef.current.value = ''
    }
  }

  const handleFileInputChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files && e.target.files.length > 0) {
      uploadFiles(e.target.files)
    }
  }

  // 剪贴板粘贴文件或图片
  const handlePaste = (e: React.ClipboardEvent<HTMLTextAreaElement>) => {
    if (e.clipboardData && e.clipboardData.files && e.clipboardData.files.length > 0) {
      e.preventDefault()
      uploadFiles(e.clipboardData.files)
    }
  }

  // 拖拽文件进入聊天框
  const handleDragOver = (e: React.DragEvent) => {
    e.preventDefault()
    e.stopPropagation()
    setIsDraggingOver(true)
  }

  const handleDragLeave = (e: React.DragEvent) => {
    e.preventDefault()
    e.stopPropagation()
    setIsDraggingOver(false)
  }

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault()
    e.stopPropagation()
    setIsDraggingOver(false)
    if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length > 0) {
      uploadFiles(e.dataTransfer.files)
    }
  }

  // 滚动位置管理：流式吸底与用户自由翻阅保护
  const userScrolledUpRef = useRef<boolean>(false)
  const isProgrammaticScrollRef = useRef<boolean>(false)
  const lastScrollTopRef = useRef<number>(0)

  // 监听鼠标滚轮/触控板意图：感知向上滑动
  const handleWheel = (e: React.WheelEvent) => {
    if (e.deltaY < 0) {
      // 只要用户向上滚轮，立即解除吸底跟随，允许向上查阅历史
      userScrolledUpRef.current = true
    } else if (e.deltaY > 0) {
      // 用户向下滚轮：检查是否已接近底部 (<= 40px)
      const el = messagesContainerRef.current
      if (el) {
        const dist = el.scrollHeight - el.scrollTop - el.clientHeight
        if (dist <= 40) {
          userScrolledUpRef.current = false
          setShowScrollBottom(false)
        }
      }
    }
  }

  // 监听容器滚动事件：精准感知拖动滚动条、滑轮翻阅或触控板滑动
  const handleScroll = () => {
    const el = messagesContainerRef.current
    if (!el) return

    // 如果是程序内部主动置底触发的滚动，更新 lastScrollTop 后直接返回
    if (isProgrammaticScrollRef.current) {
      lastScrollTopRef.current = el.scrollTop
      return
    }

    const currentScrollTop = el.scrollTop
    const isScrollingUp = currentScrollTop < lastScrollTopRef.current
    lastScrollTopRef.current = currentScrollTop

    const distanceFromBottom = el.scrollHeight - currentScrollTop - el.clientHeight

    // 1. 焦点跟随控制：向上滑动暂停跟随；滑至底部 (<= 40px) 恢复跟随
    if (isScrollingUp) {
      userScrolledUpRef.current = true
    } else if (distanceFromBottom <= 40) {
      userScrolledUpRef.current = false
    }

    // 2. 浮动“直达最新”按钮防抖迟滞区间 (Hysteresis):
    // 只有偏离底部超过 160px 时才展示浮动按钮（彻底根绝轻微滚轮晃动时按钮不停上弹下沉的抖动）；滑回 50px 以内才隐藏
    if (distanceFromBottom > 160) {
      setShowScrollBottom(true)
    } else if (distanceFromBottom <= 50) {
      setShowScrollBottom(false)
    }

    // 3. 向上滑动到顶部附近自动触发拉取更早历史会话
    if (currentScrollTop <= 60 && hasMoreHistory && !isLoadingMoreHistory) {
      loadEarlierHistory()
    }
  }

  // 向上滚动无感拉取更早历史会话并平滑校准视口高度
  const loadEarlierHistory = async () => {
    if (isLoadingMoreHistory || !hasMoreHistory || messages.length === 0 || !activeBotId) return
    const firstMsg = messages[0]
    if (!firstMsg || !firstMsg.id) return

    setIsLoadingMoreHistory(true)
    const el = messagesContainerRef.current
    const prevScrollHeight = el ? el.scrollHeight : 0
    const prevScrollTop = el ? el.scrollTop : 0

    try {
      const res = await fetch(`/api/agent/bots/${activeBotId}/history?limit=20&before_id=${encodeURIComponent(firstMsg.id)}`)
      if (!res.ok) throw new Error('拉取更早历史失败')
      const data = await res.json()
      if (data.history && data.history.length > 0) {
        const olderMessages: ChatMessage[] = data.history.map((h: any) => ({
          id: h.id || `msg-${Math.random()}`,
          role: h.role,
          content: h.content,
          attachments: h.metadata?.attachments || [],
          thought: h.metadata?.thought || '',
          codeBlocks: (h.metadata?.code_blocks || []).map((c: any) => ({
            ...c,
            status: c.status === 'error' ? ('error' as const) : ('success' as const),
          })),
          executionLogs: (h.metadata?.execution_logs || []).map((l: any) => ({
            ...l,
            status: l.status === 'error' ? ('error' as const) : ('success' as const),
          })),
          toolCalls: h.metadata?.tool_calls || [],
          orchestrations: h.metadata?.orchestrations || [],
          metadata: h.metadata || {},
          timestamp: h.timestamp,
          isStreaming: false,
          streamStatus: 'done',
        }))

        setMessages((prev) => [...olderMessages, ...prev])
        setHasMoreHistory(Boolean(data.has_more))
        setTotalHistoryCount(data.total || 0)

        // 保持视口当前位置无感知跳动 (Scroll Anchor Correction)
        requestAnimationFrame(() => {
          if (messagesContainerRef.current) {
            const newScrollHeight = messagesContainerRef.current.scrollHeight
            const diff = newScrollHeight - prevScrollHeight
            isProgrammaticScrollRef.current = true
            messagesContainerRef.current.scrollTop = prevScrollTop + diff
            lastScrollTopRef.current = messagesContainerRef.current.scrollTop
            requestAnimationFrame(() => {
              isProgrammaticScrollRef.current = false
            })
          }
        })
      } else {
        setHasMoreHistory(false)
      }
    } catch (err) {
      console.error('Failed to load earlier history:', err)
    } finally {
      setIsLoadingMoreHistory(false)
    }
  }

  const scrollToBottom = () => {
    userScrolledUpRef.current = false
    setShowScrollBottom(false)
    const el = messagesContainerRef.current
    if (el) {
      isProgrammaticScrollRef.current = true
      el.scrollTop = el.scrollHeight
      lastScrollTopRef.current = el.scrollHeight
      requestAnimationFrame(() => {
        isProgrammaticScrollRef.current = false
      })
    }
  }

  // 流式输出与焦点跟随：只有当用户处于底部时才跟随最新输出；用户回看历史时绝不打断强行拉回
  useEffect(() => {
    if (!userScrolledUpRef.current) {
      const el = messagesContainerRef.current
      if (el) {
        isProgrammaticScrollRef.current = true
        el.scrollTop = el.scrollHeight
        lastScrollTopRef.current = el.scrollHeight
        requestAnimationFrame(() => {
          isProgrammaticScrollRef.current = false
        })
      }
    }
  }, [messages, isGenerating])

  const handleRegenerate = async (msgId: string) => {
    if (isGenerating) return

    // 1. 本地界面移除当前助手消息
    setMessages((prev) => prev.filter((m) => m.id !== msgId))

    // 2. 准备接收流式生成
    const assistantMsgId = `ast-${Date.now()}`
    const placeholderMsg: ChatMessage = {
      id: assistantMsgId,
      role: 'assistant',
      content: '',
      isStreaming: true,
      streamStatus: 'thinking',
      currentStep: 1,
      startTime: Date.now(),
    }
    setMessages((prev) => [...prev, placeholderMsg])
    setIsGenerating(true)

    const controller = new AbortController()
    abortControllerRef.current = controller

    try {
      const response = await fetch(`/api/agent/bots/${activeBotId}/regenerate`, {
        method: 'POST',
        signal: controller.signal,
      })
      if (!response.ok) {
        const errData = await response.json().catch(() => ({}))
        throw new Error(errData.detail || `HTTP ${response.status}`)
      }
      await consumeSSEStream(response, assistantMsgId, activeBotId, controller)
    } catch (err: any) {
      if (err.name !== 'AbortError') {
        showToast({ type: 'error', message: err.message || '重新生成失败' })
        setMessages((prev) =>
          prev.map((msg) =>
            msg.id === assistantMsgId
              ? { ...msg, isStreaming: false, streamStatus: 'error', content: `[重新生成异常] ${err.message}` }
              : msg
          )
        )
      }
      setIsGenerating(false)
    }
  }

  const handleEditUserMessage = (msg: ChatMessage) => {
    setInputMessage(msg.content)
    if (msg.attachments && msg.attachments.length > 0) {
      setAttachedFiles(msg.attachments)
    }
    showToast({ type: 'info', message: '已载入输入框，修改后回车即可重新发送' })
    setTimeout(() => {
      const textarea = document.querySelector('textarea') as HTMLTextAreaElement | null
      if (textarea) {
        textarea.focus()
        textarea.setSelectionRange(textarea.value.length, textarea.value.length)
      }
    }, 50)
  }

  // 产出物与文件管理中心状态
  const [botFiles, setBotFiles] = useState<BotFileItem[]>([])
  const [isLoadingFiles, setIsLoadingFiles] = useState<boolean>(false)
  const [isCleaningFiles, setIsCleaningFiles] = useState<boolean>(false)

  const fetchBotFiles = useCallback(async (botId?: string, requestEpoch = botRequestEpochRef.current) => {
    const targetId = botId || activeBotId
    if (!targetId) return
    setIsLoadingFiles(true)
    try {
      const res = await fetch(`/api/agent/bots/${targetId}/files`)
      if (res.ok) {
        const data = await res.json()
        if (isCurrentBotRequest(targetId, requestEpoch)) setBotFiles(data.files || [])
      }
    } catch (e) {
      console.error('Failed to fetch bot files', e)
    } finally {
      if (isCurrentBotRequest(targetId, requestEpoch)) setIsLoadingFiles(false)
    }
  }, [activeBotId])

  const handleDeleteBotFile = async (filename: string) => {
    if (!confirm(`确定要彻底删除文件「${filename}」吗？`)) return
    try {
      const res = await fetch(`/api/agent/bots/${activeBotId}/files/${encodeURIComponent(filename)}`, {
        method: 'DELETE',
      })
      if (res.ok) {
        showToast({ type: 'success', message: '文件已彻底删除' })
        fetchBotFiles()
        loadBotDetails(activeBotId)
      } else {
        showToast({ type: 'error', message: '删除文件失败' })
      }
    } catch {
      showToast({ type: 'error', message: '删除网络异常' })
    }
  }

  const handleCleanupOrphanFiles = async () => {
    setIsCleaningFiles(true)
    try {
      const res = await fetch(`/api/agent/bots/${activeBotId}/files/cleanup`, { method: 'POST' })
      if (res.ok) {
        const data = await res.json()
        showToast({ type: 'success', message: `已成功清理 ${data.cleaned_count || 0} 个无用孤儿文件` })
        fetchBotFiles()
        loadBotDetails(activeBotId)
      } else {
        showToast({ type: 'error', message: '清理失败' })
      }
    } catch {
      showToast({ type: 'error', message: '清理网络异常' })
    } finally {
      setIsCleaningFiles(false)
    }
  }

  const handleExportAllFiles = () => {
    downloadWithAuth(`/api/agent/bots/${activeBotId}/files-export`, `${activeBotId}_files.zip`).catch((error) =>
      showToast({ type: 'error', message: error.message || '下载失败' })
    )
  }

  // 资产右侧抽屉 / Tab
  const [activeTab, setActiveTab] = useState<'prompt' | 'guidelines' | 'skills' | 'memory' | 'files' | 'knowledge'>('prompt')
  const [isDrawerOpen, setIsDrawerOpen] = useState<boolean>(false)
  const [showTopologyModal, setShowTopologyModal] = useState<boolean>(false)

  // 响应式屏幕尺寸与抽屉自适应状态
  const [windowWidth, setWindowWidth] = useState<number>(() =>
    typeof window !== 'undefined' ? window.innerWidth : 1280
  )
  const isMobile = windowWidth < 1024
  const isCompact = windowWidth < 1440
  const [isMobileListOpen, setIsMobileListOpen] = useState<boolean>(false)

  useEffect(() => {
    const handleResize = () => setWindowWidth(window.innerWidth)
    window.addEventListener('resize', handleResize)
    return () => window.removeEventListener('resize', handleResize)
  }, [])

  // 知识库管理状态 (Enterprise Knowledge Base / RAG)
  const [knowledgeDocs, setKnowledgeDocs] = useState<KnowledgeDoc[]>([])
  const [isLoadingKnowledge, setIsLoadingKnowledge] = useState<boolean>(false)
  const [isUploadingKnowledge, setIsUploadingKnowledge] = useState<boolean>(false)
  const [knowledgeSearchQuery, setKnowledgeSearchQuery] = useState<string>('')
  const [knowledgeSearchResults, setKnowledgeSearchResults] = useState<any[]>([])
  const [isSearchingKnowledge, setIsSearchingKnowledge] = useState<boolean>(false)
  const [hasSearchedKnowledge, setHasSearchedKnowledge] = useState<boolean>(false)
  const knowledgeFileInputRef = useRef<HTMLInputElement>(null)

  // 获取知识库文档
  const fetchKnowledgeDocs = useCallback(async (botId?: string, requestEpoch = botRequestEpochRef.current) => {
    const targetId = botId || activeBotId
    if (!targetId) return
    try {
      setIsLoadingKnowledge(true)
      const res = await fetch(`/api/agent/bots/${targetId}/knowledge/docs`)
      if (res.ok) {
        const data = await res.json()
        if (isCurrentBotRequest(targetId, requestEpoch)) setKnowledgeDocs(data.docs || [])
      }
    } catch (e) {
      console.error('Failed to fetch knowledge docs:', e)
    } finally {
      if (isCurrentBotRequest(targetId, requestEpoch)) setIsLoadingKnowledge(false)
    }
  }, [activeBotId])

  // 上传知识库文档
  const handleUploadKnowledge = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (!file || !activeBotId) return
    try {
      setIsUploadingKnowledge(true)
      const formData = new FormData()
      formData.append('file', file)
      const res = await fetch(`/api/agent/bots/${activeBotId}/knowledge/upload`, {
        method: 'POST',
        body: formData,
      })
      if (res.ok) {
        await fetchKnowledgeDocs()
      } else {
        const err = await res.json()
        alert(err.detail || '上传知识库文档失败')
      }
    } catch (e) {
      console.error('Upload knowledge error:', e)
      alert('上传知识库文档异常')
    } finally {
      setIsUploadingKnowledge(false)
      if (e.target) e.target.value = ''
    }
  }

  // 切换文档启用/禁用
  const handleToggleKnowledgeDoc = async (docId: string, currentEnabled: boolean) => {
    try {
      const res = await fetch(`/api/agent/bots/${activeBotId}/knowledge/docs/${docId}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: !currentEnabled }),
      })
      if (res.ok) {
        setKnowledgeDocs((prev) =>
          prev.map((d) => (d.id === docId ? { ...d, enabled: !currentEnabled } : d))
        )
      }
    } catch (e) {
      console.error('Toggle knowledge error:', e)
    }
  }

  // 删除知识库文档
  const handleDeleteKnowledgeDoc = async (docId: string) => {
    if (!confirm('确定要删除此知识库文档及其所有索引切片吗？')) return
    try {
      const res = await fetch(`/api/agent/bots/${activeBotId}/knowledge/docs/${docId}`, {
        method: 'DELETE',
      })
      if (res.ok) {
        setKnowledgeDocs((prev) => prev.filter((d) => d.id !== docId))
      }
    } catch (e) {
      console.error('Delete knowledge doc error:', e)
    }
  }

  // 在线检索与召回调试
  const handleTestSearchKnowledge = async () => {
    const q = knowledgeSearchQuery.trim()
    if (!q || !activeBotId) return
    try {
      setIsSearchingKnowledge(true)
      setHasSearchedKnowledge(true)
      const res = await fetch(`/api/agent/bots/${activeBotId}/knowledge/search`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ query: q, top_k: 4 }),
      })
      if (res.ok) {
        const data = await res.json()
        setKnowledgeSearchResults(data.results || [])
      }
    } catch (e) {
      console.error('Search knowledge error:', e)
    } finally {
      setIsSearchingKnowledge(false)
    }
  }

  // 图片点击大图全屏预览弹窗状态 (Image Lightbox)
  const [previewImage, setPreviewImage] = useState<{ url: string; name?: string } | null>(null)

  // 监听键盘 Escape 键关闭大图预览
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        setPreviewImage(null)
      }
    }
    window.addEventListener('keydown', handleKeyDown)
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [])

  const [isSavingBasePrompt, setIsSavingBasePrompt] = useState<boolean>(false)

  // 门禁与导出状态
  const [isResolvingApproval, setIsResolvingApproval] = useState<string | null>(null)
  const [showExportMenu, setShowExportMenu] = useState<boolean>(false)
  const exportMenuRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const handleClickOutside = (e: MouseEvent) => {
      if (exportMenuRef.current && !exportMenuRef.current.contains(e.target as Node)) {
        setShowExportMenu(false)
      }
    }
    document.addEventListener('mousedown', handleClickOutside)
    return () => document.removeEventListener('mousedown', handleClickOutside)
  }, [])

  const handleApprovalDecision = async (approvalId: string, decision: 'approve' | 'reject') => {
    try {
      setIsResolvingApproval(approvalId)
      const res = await fetch(`/api/agent/bots/${activeBotId}/approvals/${approvalId}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ decision }),
      })
      if (!res.ok) {
        const errData = await res.json().catch(() => ({}))
        throw new Error(errData.detail || '提交授权决议失败')
      }
      showToast({
        type: decision === 'approve' ? 'success' : 'info',
        message: decision === 'approve' ? '已授权高危操作执行' : '已拒绝此高危操作',
      })
      setMessages((prev) =>
        prev.map((msg) => {
          if (msg.pendingApproval?.approval_id === approvalId) {
            return {
              ...msg,
              pendingApproval: { ...msg.pendingApproval, decision },
              streamStatus: decision === 'approve' ? 'executing' : msg.streamStatus,
            }
          }
          return msg
        })
      )
    } catch (err: any) {
      showToast({ type: 'error', message: err.message || '操作失败' })
    } finally {
      setIsResolvingApproval(null)
    }
  }

  const [isExporting, setIsExporting] = useState<boolean>(false)

  const handleExportChat = async (format: 'markdown' | 'html') => {
    setShowExportMenu(false)
    if (messages.length === 0) {
      showToast({ type: 'info', message: '当前会话暂无记录可供导出' })
      return
    }

    try {
      setIsExporting(true)
      showToast({
        type: 'info',
        message: `正在生成 ${format === 'markdown' ? 'Markdown' : 'HTML'} 格式排障报告...`,
      })

      const res = await fetch(`/api/agent/bots/${activeBotId}/export?format=${format}`)
      if (!res.ok) {
        let errMsg = '导出失败'
        try {
          const errData = await res.json()
          errMsg = errData.detail || errMsg
        } catch {
          // ignore
        }
        showToast({ type: 'error', message: errMsg })
        return
      }

      // 获取服务端指定的规范文件名（支持中文字符与时间戳）
      let filename = `${activeBotId}_report_${Date.now()}.${format === 'markdown' ? 'md' : 'html'}`
      const disposition = res.headers.get('Content-Disposition')
      if (disposition) {
        const utf8Match = disposition.match(/filename\*=utf-8''([^;]+)/i)
        if (utf8Match && utf8Match[1]) {
          filename = decodeURIComponent(utf8Match[1])
        } else {
          const standardMatch = disposition.match(/filename="?([^";]+)"?/i)
          if (standardMatch && standardMatch[1]) {
            filename = standardMatch[1]
          }
        }
      }

      const blob = await res.blob()
      const blobUrl = window.URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = blobUrl
      link.setAttribute('download', filename)
      document.body.appendChild(link)
      link.click()

      setTimeout(() => {
        if (document.body.contains(link)) {
          document.body.removeChild(link)
        }
        window.URL.revokeObjectURL(blobUrl)
      }, 1000)

      showToast({
        type: 'success',
        message: `报告「${filename}」已生成并开始下载`,
      })
    } catch (err: any) {
      console.error('Export chat failed:', err)
      showToast({ type: 'error', message: `导出异常: ${err.message || '网络连接失败'}` })
    } finally {
      setIsExporting(false)
    }
  }

  const handlePreviewHtml = () => {
    setShowExportMenu(false)
    if (messages.length === 0) {
      showToast({ type: 'info', message: '当前会话暂无记录可供预览' })
      return
    }
    openWithAuth(`/api/agent/bots/${activeBotId}/export?format=html&preview=true`).catch((error) =>
      showToast({ type: 'error', message: error.message || '预览失败' })
    )
  }

  // 编辑智能体资料弹窗 (名称、头像、描述)
  const [isEditProfileModalOpen, setIsEditProfileModalOpen] = useState<boolean>(false)
  const [editProfileData, setEditProfileData] = useState({ name: '', avatar: '🤖', description: '' })
  const [isSavingProfile, setIsSavingProfile] = useState<boolean>(false)

  // 复制反馈
  const [copiedCodeId, setCopiedCodeId] = useState<string | null>(null)

  // 弹窗状态
  const [isCreateBotModalOpen, setIsCreateBotModalOpen] = useState<boolean>(false)
  const [newBotData, setNewBotData] = useState({ name: '', goal: '', avatar: '🤖' })
  const [userSelectedAvatar, setUserSelectedAvatar] = useState<boolean>(false)
  const [botTemplates, setBotTemplates] = useState<any[]>([])
  const [selectedTemplateId, setSelectedTemplateId] = useState<string | null>(null)
  const [isCreatingBot, setIsCreatingBot] = useState<boolean>(false)
  const [createProgressStage, setCreateProgressStage] = useState<number>(0)
  const [isSkillModalOpen, setIsSkillModalOpen] = useState<boolean>(false)
  const [inspectingSkill, setInspectingSkill] = useState<SkillItem | null>(null)

  // 沉淀技能弹窗
  const [isSaveSkillModalOpen, setIsSaveSkillModalOpen] = useState<boolean>(false)
  const [skillToSave, setSkillToSave] = useState({ name: '', description: '', python_code: '' })

  // 模型配置
  const [isModelModalOpen, setIsModelModalOpen] = useState<boolean>(false)

  const [ownershipSubject, setOwnershipSubject] = useState('')

  const handleTransferOwnership = async () => {
    const subject = ownershipSubject.trim()
    if (!activeBotId || !subject) return
    try {
      const res = await fetch(`/api/agent/bots/${activeBotId}/ownership`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ owner_id: subject }),
      })
      const data = await res.json()
      if (!res.ok) throw new Error(data.detail || '转移失败')
      showToast({ type: 'success', message: 'BOT 所有权已转移' })
      setOwnershipSubject('')
      await fetchBots()
      await loadBotDetails(activeBotId)
    } catch (error: any) {
      showToast({ type: 'error', message: error.message || '转移失败' })
    }
  }


  // 二次确认删除智能体弹窗
  const [isDeleteConfirmModalOpen, setIsDeleteConfirmModalOpen] = useState<boolean>(false)
  const [deleteConfirmInput, setDeleteConfirmInput] = useState<string>('')
  const [isDeletingBot, setIsDeletingBot] = useState<boolean>(false)

  // @智能体 协同指挥下拉
  const [isMentionDropdownOpen, setIsMentionDropdownOpen] = useState<boolean>(false)
  const [mentionFilter, setMentionFilter] = useState<string>('')
  const [selectedMentionIndex, setSelectedMentionIndex] = useState<number>(0)
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const mentionDropdownRef = useRef<HTMLDivElement>(null)
  const [llmConfig, setLlmConfig] = useState<LLMConfigData>({
    provider: 'deepseek',
    model_name: 'deepseek-chat',
    base_url: 'https://api.deepseek.com',
    temperature: 0.2,
    max_steps: 30,
    context_budget: 32000,
    max_context_turns: 25,
  })
  const [inputApiKey, setInputApiKey] = useState<string>('')
  const [isTestingModel, setIsTestingModel] = useState<boolean>(false)
  const [availableModels, setAvailableModels] = useState<string[]>([])
  const [isFetchingModels, setIsFetchingModels] = useState<boolean>(false)
  const [modelFetchMsg, setModelFetchMsg] = useState<{ type: 'success' | 'warn'; text: string } | null>(null)
  const [isAdminUser, setIsAdminUser] = useState(false)

  useEffect(() => {
    fetch('/api/auth/me')
      .then((response) => response.ok ? response.json() : null)
      .then((data) => setIsAdminUser(Boolean(data?.is_admin)))
      .catch(() => setIsAdminUser(false))
  }, [])


  const fetchAvailableModels = async (showNotification = false, overrideConfig?: Partial<LLMConfigData>) => {
    setIsFetchingModels(true)
    try {
      const targetProvider = overrideConfig?.provider || llmConfig.provider
      const targetBaseUrl = overrideConfig?.base_url !== undefined ? overrideConfig.base_url : llmConfig.base_url
      const targetKey = inputApiKey || (llmConfig.has_api_key ? undefined : '')

      const res = await fetch('/api/agent/models', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          provider: targetProvider,
          base_url: targetBaseUrl,
          api_key: targetKey,
        }),
      })
      const data = await res.json()
      if (data && data.models && data.models.length > 0) {
        setAvailableModels(data.models)
        if (data.source === 'remote') {
          setModelFetchMsg({ type: 'success', text: `已探测到 ${data.models.length} 个模型` })
          if (showNotification) {
            showToast({ type: 'success', message: `成功从 /models 探测到 ${data.models.length} 个可用模型！` }, 3500)
          }
        } else {
          setModelFetchMsg({ type: 'warn', text: '已载入常用预设' })
          if (showNotification) {
            showToast({ type: 'warning', message: data.message || '已提供常见常用模型候选' }, 4000)
          }
        }
      }
    } catch (e: any) {
      if (showNotification) {
        showToast({ type: 'error', message: '拉取可用模型失败: ' + (e.message || '网络异常') })
      }
    } finally {
      setIsFetchingModels(false)
    }
  }

  // 打开模型弹窗时自动拉取可用模型
  useEffect(() => {
    if (isModelModalOpen) {
      fetchAvailableModels(false)
    }
  }, [isModelModalOpen])

  // 动态推荐指令刷新状态
  const [isRefreshingPrompts, setIsRefreshingPrompts] = useState<boolean>(false)

  const fetchBotTemplates = async () => {
    try {
      const res = await fetch('/api/agent/templates')
      const data = await res.json()
      if (data && data.templates) {
        setBotTemplates(data.templates)
      }
    } catch (e) {
      console.error('Failed to fetch bot templates:', e)
    }
  }

  // 初始化拉取 BOT 列表、LLM 配置与角色模板
  useEffect(() => {
    fetchBots()
    fetchLlmConfig()
    fetchBotTemplates()
  }, [])

  // 切换 BOT 时拉取详情与历史
  useEffect(() => {
    if (activeBotId) {
      activeBotIdRef.current = activeBotId
      const requestEpoch = ++botRequestEpochRef.current
      if (abortControllerRef.current) {
        abortControllerRef.current.abort()
        abortControllerRef.current = null
      }
      setIsGenerating(false)
      loadBotDetails(activeBotId, requestEpoch)
      loadBotHistory(activeBotId, requestEpoch)
    }
  }, [activeBotId])

  // 创建智能体期间，屏蔽 ESC 键关闭窗口
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && isCreateBotModalOpen) {
        if (isCreatingBot) {
          e.preventDefault()
          e.stopPropagation()
        } else {
          setIsCreateBotModalOpen(false)
        }
      }
    }
    window.addEventListener('keydown', handleKeyDown)
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [isCreateBotModalOpen, isCreatingBot])


  // 待协同的智能体列表（过滤出匹配项）
  const filteredMentionBots = useMemo(() => {
    const kw = mentionFilter.trim().toLowerCase()
    return bots.filter((b) => {
      if (!kw) return true
      return (
        b.name.toLowerCase().includes(kw) ||
        b.id.toLowerCase().includes(kw) ||
        (b.description && b.description.toLowerCase().includes(kw))
      )
    })
  }, [bots, mentionFilter])

  // 点击外部关闭 @智能体 协同下拉框
  useEffect(() => {
    const handleClickOutside = (e: MouseEvent) => {
      if (
        mentionDropdownRef.current &&
        !mentionDropdownRef.current.contains(e.target as Node) &&
        textareaRef.current &&
        !textareaRef.current.contains(e.target as Node)
      ) {
        setIsMentionDropdownOpen(false)
      }
    }
    document.addEventListener('mousedown', handleClickOutside)
    return () => document.removeEventListener('mousedown', handleClickOutside)
  }, [])


  const fetchBots = async () => {
    try {
      const res = await fetch('/api/agent/bots')
      const data = await res.json()
      if (data.bots) {
        setBots(data.bots)
        if (!data.bots.some((b: BotSummary) => b.id === activeBotId) && data.bots.length > 0) {
          setActiveBotId(data.bots[0].id)
        }
      }
      fetchActiveRuns()
    } catch (e) {
      console.error('Failed to fetch bots:', e)
    }
  }

  const loadBotDetails = async (botId: string, requestEpoch = botRequestEpochRef.current) => {
    try {
      const res = await fetch(`/api/agent/bots/${botId}`)
      const data = await res.json()
      if (data.bot && isCurrentBotRequest(botId, requestEpoch)) {
        setActiveBot(data.bot)
      }
      if (isCurrentBotRequest(botId, requestEpoch)) {
        fetchBotFiles(botId, requestEpoch)
        fetchKnowledgeDocs(botId, requestEpoch)
      }
    } catch (e) {
      console.error('Failed to load bot details:', e)
    } finally {
      console.debug('Bot details loaded:', botId)
    }
  }

  // 消费 SSE 数据流并实时驱动思维链与界面更新
  const consumeSSEStream = async (
    response: Response,
    assistantMsgId: string,
    botId: string,
    controller: AbortController
  ) => {
    const reader = response.body?.getReader()
    const decoder = new TextDecoder('utf-8')
    let buffer = ''
    let terminalEventReceived = false

    if (!reader) throw new Error('无法读取响应流')

    try {
      while (true) {
        const { value, done } = await reader.read()
        if (done) break
        if (abortControllerRef.current !== controller || activeBotIdRef.current !== botId) return

        buffer += decoder.decode(value, { stream: true })
        const lines = buffer.split('\n')
        buffer = lines.pop() || ''

        let currentEvent = 'message'

        for (const line of lines) {
          if (abortControllerRef.current !== controller || activeBotIdRef.current !== botId) return
          if (line.startsWith('event: ')) {
            currentEvent = line.slice(7).trim()
          } else if (line.startsWith('data: ')) {
            const rawData = line.slice(6).trim()
            if (!rawData) continue

            try {
              const payload = JSON.parse(rawData)

              setMessages((prev) =>
                prev.map((msg) => {
                  if (msg.id !== assistantMsgId) return msg

                  const updated = { ...msg }

                  if (currentEvent === 'sync') {
                    const r = payload.run || payload
                    if (r.thought) updated.thought = r.thought
                    if (r.code_blocks && Array.isArray(r.code_blocks)) {
                      updated.codeBlocks = r.code_blocks.map((c: any) => ({
                        ...c,
                        status: c.status === 'error' ? 'error' : c.status === 'running' ? 'running' : 'success',
                      }))
                    }
                    if (r.execution_logs && Array.isArray(r.execution_logs)) {
                      updated.executionLogs = r.execution_logs.map((l: any) => ({
                        ...l,
                        status: l.status === 'error' ? 'error' : 'success',
                      }))
                    }
                    if (r.content) updated.content = r.content
                    if (r.stream_status) updated.streamStatus = r.stream_status
                    if (r.current_step) updated.currentStep = r.current_step
                    if (r.pending_approval !== undefined) updated.pendingApproval = r.pending_approval
                    if (r.status === 'done' || r.status === 'error' || r.status === 'stopped') {
                      terminalEventReceived = true
                      updated.isStreaming = false
                      updated.streamStatus = r.status === 'error' ? 'error' : 'done'
                      setActiveRunsMap((prev) => {
                        if (!prev[botId]) return prev
                        const next = { ...prev }
                        delete next[botId]
                        return next
                      })
                    } else {
                      setActiveRunsMap((prev) => ({
                        ...prev,
                        [botId]: {
                          run_id: r.run_id || prev[botId]?.run_id || '',
                          bot_id: botId,
                          status: 'running',
                          stream_status: r.stream_status || 'thinking',
                          start_time: r.start_time,
                          current_step: r.current_step || 1,
                          user_message: r.user_message || prev[botId]?.user_message || '',
                        },
                      }))
                    }
                  } else if (currentEvent === 'thought_chunk') {
                    const delta = payload.delta || ''
                    if (delta) {
                      updated.thought = (updated.thought || '') + delta
                      updated.streamStatus = 'thinking'
                    }
                  } else if (currentEvent === 'thought') {
                    const clean = (payload.content || '').trim()
                    if (clean) {
                      updated.thought = (updated.thought ? updated.thought + '\n\n' : '') + clean
                    }
                  } else if (currentEvent === 'code_start') {
                    updated.streamStatus = 'executing'
                    updated.currentStep = payload.step || 1
                    setActiveRunsMap((prev) => {
                      if (!prev[botId]) return prev
                      return {
                        ...prev,
                        [botId]: {
                          ...prev[botId],
                          stream_status: 'executing',
                          current_step: payload.step || prev[botId].current_step || 1,
                        },
                      }
                    })
                    const existingCodes = [...(updated.codeBlocks || [])]
                    const idx = existingCodes.findIndex((c) => (c.step || 1) === (payload.step || 1))
                    const item = { step: payload.step || 1, code: payload.code || '', status: 'running' as const }
                    if (idx >= 0) {
                      existingCodes[idx] = item
                    } else {
                      existingCodes.push(item)
                    }
                    updated.codeBlocks = existingCodes
                  } else if (currentEvent === 'code') {
                    updated.codeBlocks = [
                      ...(updated.codeBlocks || []),
                      { step: payload.step || 1, code: payload.code || '', status: 'running' as const },
                    ]
                  } else if (currentEvent === 'execution_result') {
                    const stepNum = payload.step || 1
                    const existingLogs = [...(updated.executionLogs || [])]
                    const logIdx = existingLogs.findIndex((l) => (l.step || 1) === stepNum)
                    const logItem = { step: stepNum, output: payload.output || '', status: 'success' as const }
                    if (logIdx >= 0) {
                      existingLogs[logIdx] = logItem
                    } else {
                      existingLogs.push(logItem)
                    }
                    updated.executionLogs = existingLogs

                    if (updated.codeBlocks) {
                      updated.codeBlocks = updated.codeBlocks.map((c) =>
                        (c.step || 1) === stepNum ? { ...c, status: 'success' as const } : c
                      )
                    }
                  } else if (currentEvent === 'execution' || currentEvent === 'tool') {
                    const stepNum = payload.step || 1
                    updated.executionLogs = [
                      ...(updated.executionLogs || []),
                      { step: stepNum, output: payload.output || '', status: 'success' as const },
                    ]
                    if (updated.codeBlocks) {
                      updated.codeBlocks = updated.codeBlocks.map((c) =>
                        (c.step || 1) === stepNum ? { ...c, status: 'success' as const } : c
                      )
                    }
                  } else if (currentEvent === 'approval_required') {
                    updated.pendingApproval = payload
                    updated.streamStatus = 'waiting_approval'
                    setActiveRunsMap((prev) => {
                      if (!prev[botId]) return prev
                      return {
                        ...prev,
                        [botId]: {
                          ...prev[botId],
                          stream_status: 'waiting_approval',
                          pending_approval: payload,
                        },
                      }
                    })
                  } else if (currentEvent === 'approval_resolved') {
                    if (updated.pendingApproval && updated.pendingApproval.approval_id === payload.approval_id) {
                      updated.pendingApproval = {
                        ...updated.pendingApproval,
                        decision: payload.decision,
                      }
                    }
                    if (payload.decision === 'approve') {
                      updated.streamStatus = 'executing'
                    }
                    setActiveRunsMap((prev) => {
                      if (!prev[botId]) return prev
                      return {
                        ...prev,
                        [botId]: {
                          ...prev[botId],
                          stream_status: payload.decision === 'approve' ? 'executing' : prev[botId].stream_status,
                          pending_approval: undefined,
                        },
                      }
                    })
                  } else if (currentEvent === 'orchestration') {
                    const existingOrchs = [...(updated.orchestrations || [])]
                    existingOrchs.push(payload)
                    updated.orchestrations = existingOrchs
                  } else if (currentEvent === 'answer_start') {
                    updated.streamStatus = 'answering'
                    setActiveRunsMap((prev) => {
                      if (!prev[botId]) return prev
                      return {
                        ...prev,
                        [botId]: {
                          ...prev[botId],
                          stream_status: 'answering',
                        },
                      }
                    })
                    if (updated.codeBlocks) {
                      updated.codeBlocks = updated.codeBlocks.map((c) =>
                        c.status === 'running' ? { ...c, status: 'success' as const } : c
                      )
                    }
                  } else if (currentEvent === 'chunk' || currentEvent === 'message') {
                    let text = currentEvent === 'chunk' ? (payload.delta || '') : (payload.content || '')
                    updated.streamStatus = 'answering'
                    if (updated.codeBlocks) {
                      updated.codeBlocks = updated.codeBlocks.map((c) =>
                        c.status === 'running' ? { ...c, status: 'success' as const } : c
                      )
                    }
                    if (text.includes('<think>')) {
                      const thinkMatch = text.match(/<think>([\s\S]*?)(?:<\/think>|$)/i)
                      if (thinkMatch) {
                        const extracted = thinkMatch[1].trim()
                        if (extracted) {
                          updated.thought = (updated.thought ? updated.thought + '\n\n' : '') + extracted
                        }
                        text = text.replace(/<think>[\s\S]*?(?:<\/think>|$)/gi, '').trim()
                      }
                    }
                    if (currentEvent === 'chunk') {
                      updated.content = (updated.content || '') + text
                    } else {
                      updated.content = text || updated.content
                    }
                  } else if (currentEvent === 'done') {
                    terminalEventReceived = true
                    updated.isStreaming = false
                    updated.streamStatus = 'done'
                    if (payload.orchestrations) {
                      updated.orchestrations = payload.orchestrations
                    }
                    if (payload.attachments && payload.attachments.length > 0) {
                      updated.attachments = payload.attachments
                    } else if (payload.files && payload.files.length > 0) {
                      updated.attachments = payload.files
                    }
                    setActiveRunsMap((prev) => {
                      if (!prev[botId]) return prev
                      const next = { ...prev }
                      delete next[botId]
                      return next
                    })
                    if (updated.codeBlocks) {
                      updated.codeBlocks = updated.codeBlocks.map((c) =>
                        c.status === 'running' ? { ...c, status: 'success' as const } : c
                      )
                    }
                    if (updated.startTime) {
                      updated.elapsedSeconds = Number(((Date.now() - updated.startTime) / 1000).toFixed(1))
                    }
                    // 实时联动刷新右侧抽屉「产出物与文件中心」
                    fetchBotFiles(botId)
                  } else if (currentEvent === 'error') {
                    terminalEventReceived = true
                    updated.isStreaming = false
                    updated.streamStatus = 'error'
                    updated.content = `❌ **执行遇到错误**: ${payload.error || '未知异常'}`
                    setActiveRunsMap((prev) => {
                      if (!prev[botId]) return prev
                      const next = { ...prev }
                      delete next[botId]
                      return next
                    })
                  }

                  return updated
                })
              )
            } catch (err) {
              console.warn('Error parsing SSE json:', err, rawData)
            }
          }
        }
      }
    } finally {
      // 清理全局流状态必须无条件执行。切换 Bot 后，旧流仍会进入 finally，
      // 不能因为当前视图已变化而留下 isGenerating 或旧控制器引用。
      const isCurrentController = abortControllerRef.current === controller
      if (isCurrentController) {
        setIsGenerating(false)
        abortControllerRef.current = null
      }
      if (activeBotIdRef.current !== botId || isCurrentController) {
        setActiveRunsMap((prev) => {
          if (!prev[botId]) return prev
          const next = { ...prev }
          delete next[botId]
          return next
        })
      }

      // 旧 Bot 的流只清理运行状态，不再更新当前视图中的消息或详情。
      if (activeBotIdRef.current !== botId) return

      if (!terminalEventReceived && !controller.signal.aborted) {
        setMessages((prev) =>
          prev.map((msg) => msg.id === assistantMsgId
            ? {
                ...msg,
                isStreaming: false,
                streamStatus: 'error',
                content: `${msg.content || ''}\n\n❌ 连接意外中断，请重试。`.trim(),
                codeBlocks: msg.codeBlocks?.map((c) =>
                  c.status === 'running' ? { ...c, status: 'error' as const } : c
                ),
              }
            : msg
          )
        )
      }

      setMessages((prev) =>
        prev.map((msg) => {
          if (msg.id === assistantMsgId) {
            const elapsed = msg.startTime ? Number(((Date.now() - msg.startTime) / 1000).toFixed(1)) : undefined
            return {
              ...msg,
              isStreaming: false,
              streamStatus: msg.streamStatus === 'error' ? 'error' : 'done',
              elapsedSeconds: msg.elapsedSeconds || elapsed,
              codeBlocks: msg.codeBlocks?.map((c) =>
                !terminalEventReceived && c.status === 'running'
                  ? { ...c, status: 'error' as const }
                  : terminalEventReceived && c.status === 'running'
                    ? { ...c, status: 'success' as const }
                    : c
              ),
            }
          }
          return msg
        })
      )

      // 刷新当前 BOT 的资产（可能已经自增强了提示词、写入了记忆或沉淀了新技能）
      loadBotDetails(botId, botRequestEpochRef.current)
      fetchBots()
    }
  }

  // 页面刷新或切换 BOT 时无感重连后台执行中的流
  const attachToActiveRunStream = async (botId: string, assistantMsgId: string) => {
    if (activeBotIdRef.current !== botId) return
    const controller = new AbortController()
    abortControllerRef.current = controller
    try {
      const response = await fetch(`/api/agent/bots/${botId}/chat/stream`, {
        signal: controller.signal,
      })
      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`)
      }
      if (activeBotIdRef.current !== botId || abortControllerRef.current !== controller) return
      await consumeSSEStream(response, assistantMsgId, botId, controller)
    } catch (err: any) {
      if (err.name !== 'AbortError') {
        console.error('Failed to attach to active run stream:', err)
        setMessages((prev) =>
          prev.map((msg) =>
            msg.id === assistantMsgId
              ? {
                  ...msg,
                  isStreaming: false,
                  streamStatus: 'error',
                  content: msg.content || `❌ 连接中断: ${err.message || '网络连接失败'}`,
                }
              : msg
          )
        )
      }
      if (abortControllerRef.current === controller) {
        abortControllerRef.current = null
        setIsGenerating(false)
      }
    }
  }

  const loadBotHistory = async (botId: string, requestEpoch = botRequestEpochRef.current) => {
    try {
      userScrolledUpRef.current = false
      setShowScrollBottom(false)

      const res = await fetch(`/api/agent/bots/${botId}/history?limit=30`)
      const data = await res.json()
      if (!isCurrentBotRequest(botId, requestEpoch)) return
      setHasMoreHistory(Boolean(data.has_more))
      setTotalHistoryCount(data.total || data.history?.length || 0)
      let initialMessages: ChatMessage[] = []
      if (data.history) {
        initialMessages = data.history.map((h: any) => ({
          id: h.id || `msg-${Math.random()}`,
          role: h.role,
          content: h.content,
          attachments: h.metadata?.attachments || [],
          thought: h.metadata?.thought || '',
          codeBlocks: (h.metadata?.code_blocks || []).map((c: any) => ({
            ...c,
            status: c.status === 'error' ? ('error' as const) : ('success' as const),
          })),
          executionLogs: (h.metadata?.execution_logs || []).map((l: any) => ({
            ...l,
            status: l.status === 'error' ? ('error' as const) : ('success' as const),
          })),
          toolCalls: h.metadata?.tool_calls || [],
          orchestrations: h.metadata?.orchestrations || [],
          metadata: h.metadata || {},
          timestamp: h.timestamp,
          isStreaming: false,
          streamStatus: 'done',
        }))
      }

      // 检查当前 BOT 是否存在后台正在运行的会话（刷新/切换恢复）
      let activeRunData: any = null
      try {
        const activeRes = await fetch(`/api/agent/bots/${botId}/active-run`)
        if (activeRes.ok) {
          const runJson = await activeRes.json()
          if (runJson.has_active_run && runJson.run && runJson.run.status === 'running') {
            activeRunData = runJson.run
          }
        }
      } catch (err) {
        console.warn('Failed to query active run:', err)
      }

      if (!isCurrentBotRequest(botId, requestEpoch)) return

      if (activeRunData) {
        // 当前任务仍在后台运行中：检查最后一条是否为该 user message，若不是则补全
        const lastMsg = initialMessages[initialMessages.length - 1]
        if (!lastMsg || lastMsg.role !== 'user' || lastMsg.content !== activeRunData.user_message) {
          initialMessages.push({
            id: activeRunData.user_msg_id || `usr-${Date.now()}`,
            role: 'user',
            content: activeRunData.user_message,
            attachments: activeRunData.attachments || [],
            timestamp: activeRunData.start_time
              ? new Date(activeRunData.start_time * 1000).toISOString()
              : new Date().toISOString(),
          })
        }

        const assistantMsgId = activeRunData.assistant_msg_id || `ast-${Date.now()}`
        const inProgressAssistant: ChatMessage = {
          id: assistantMsgId,
          role: 'assistant',
          content: activeRunData.content || '',
          thought: activeRunData.thought || '',
          codeBlocks: (activeRunData.code_blocks || []).map((c: any) => ({
            ...c,
            status: c.status === 'error' ? ('error' as const) : c.status === 'running' ? ('running' as const) : ('success' as const),
          })),
          executionLogs: (activeRunData.execution_logs || []).map((l: any) => ({
            ...l,
            status: l.status === 'error' ? ('error' as const) : ('success' as const),
          })),
          pendingApproval: activeRunData.pending_approval || undefined,
          toolCalls: [],
          metadata: {},
          isStreaming: true,
          streamStatus: (activeRunData.stream_status as any) || 'thinking',
          currentStep: activeRunData.current_step || 1,
          startTime: activeRunData.start_time ? activeRunData.start_time * 1000 : Date.now(),
          timestamp: new Date().toISOString(),
        }
        initialMessages.push(inProgressAssistant)

        revokeDetachedPreviewUrls(initialMessages)
        setMessages(initialMessages)
        setIsGenerating(true)
        setActiveRunsMap((prev) => ({
          ...prev,
          [botId]: {
            run_id: activeRunData.run_id,
            bot_id: botId,
            status: 'running',
            stream_status: activeRunData.stream_status || 'thinking',
            start_time: activeRunData.start_time,
            current_step: activeRunData.current_step || 1,
            user_message: activeRunData.user_message,
            pending_approval: activeRunData.pending_approval,
          },
        }))

        requestAnimationFrame(() => {
          if (messagesContainerRef.current) {
            messagesContainerRef.current.scrollTop = messagesContainerRef.current.scrollHeight
          }
        })

        // 重新接管 SSE 实时流
        if (isCurrentBotRequest(botId, requestEpoch)) attachToActiveRunStream(botId, assistantMsgId)
      } else {
        setActiveRunsMap((prev) => {
          if (!prev[botId]) return prev
          const next = { ...prev }
          delete next[botId]
          return next
        })
        revokeDetachedPreviewUrls(initialMessages)
        setMessages(initialMessages)
        setIsGenerating(false)

        requestAnimationFrame(() => {
          if (messagesContainerRef.current) {
            messagesContainerRef.current.scrollTop = messagesContainerRef.current.scrollHeight
          }
        })

        // 毫秒级竞态兜底：如果历史最后一条是 user 消息且目前没有 active run，稍后轻量再重拉一次历史
        const lastMsg = initialMessages[initialMessages.length - 1]
        if (lastMsg && lastMsg.role === 'user') {
          setTimeout(async () => {
            try {
              const retryRes = await fetch(`/api/agent/bots/${botId}/history`)
              const retryData = await retryRes.json()
              if (isCurrentBotRequest(botId, requestEpoch) && retryData.history && retryData.history.length > initialMessages.length) {
                setMessages(
                  retryData.history.map((h: any) => ({
                    id: h.id || `msg-${Math.random()}`,
                    role: h.role,
                    content: h.content,
                    thought: h.metadata?.thought || '',
                    codeBlocks: (h.metadata?.code_blocks || []).map((c: any) => ({
                      ...c,
                      status: c.status === 'error' ? ('error' as const) : ('success' as const),
                    })),
                    executionLogs: (h.metadata?.execution_logs || []).map((l: any) => ({
                      ...l,
                      status: l.status === 'error' ? ('error' as const) : ('success' as const),
                    })),
                    toolCalls: h.metadata?.tool_calls || [],
                    orchestrations: h.metadata?.orchestrations || [],
                    metadata: h.metadata || {},
                    timestamp: h.timestamp,
                    isStreaming: false,
                    streamStatus: 'done',
                  }))
                )
                requestAnimationFrame(() => {
                  if (messagesContainerRef.current) {
                    messagesContainerRef.current.scrollTop = messagesContainerRef.current.scrollHeight
                  }
                })
              }
            } catch (error) {
              console.warn('Failed to parse retry history', error)
            }
          }, 800)
        }
      }
    } catch (e) {
      console.error('Failed to load history:', e)
    }
  }

  const fetchLlmConfig = async () => {
    try {
      const res = await fetch('/api/agent/config')
      const data = await res.json()
      if (data.config) {
        setLlmConfig(data.config)
      }
    } catch (e) {
      console.error('Failed to fetch llm config:', e)
    }
  }

  // 发送消息并接收 SSE 流
  const handleSendMessage = async (textToSend?: string) => {
    const rawText = (textToSend ?? inputMessage).trim()
    const currentAttachments = [...attachedFiles]
    if ((!rawText && currentAttachments.length === 0) || isGenerating) return

    const text = rawText || (currentAttachments.length > 0 ? '请分析并处理上传的文件附件。' : '')

    setInputMessage('')
    setAttachedFiles([])
    userScrolledUpRef.current = false
    setShowScrollBottom(false)
    const userMsgId = `usr-${Date.now()}`
    const assistantMsgId = `ast-${Date.now()}`

    // 追加用户消息
    setMessages((prev) => [
      ...prev,
      {
        id: userMsgId,
        role: 'user',
        content: text,
        attachments: currentAttachments,
        timestamp: new Date().toISOString(),
      },
      {
        id: assistantMsgId,
        role: 'assistant',
        content: '',
        thought: '',
        codeBlocks: [],
        executionLogs: [],
        toolCalls: [],
        isStreaming: true,
        streamStatus: 'thinking',
        currentStep: 1,
        startTime: Date.now(),
        timestamp: new Date().toISOString(),
      },
    ])

    setIsGenerating(true)
    setActiveRunsMap((prev) => ({
      ...prev,
      [activeBotId]: {
        run_id: `run-${assistantMsgId}`,
        bot_id: activeBotId,
        status: 'running',
        stream_status: 'thinking',
        start_time: Date.now() / 1000,
        user_message: text,
        current_step: 1,
      },
    }))
    const controller = new AbortController()
    abortControllerRef.current = controller

    try {
      const response = await fetch(`/api/agent/bots/${activeBotId}/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          message: text,
          client_msg_id: assistantMsgId,
          attachments: currentAttachments.length > 0 ? currentAttachments : undefined,
        }),
        signal: controller.signal,
      })

      if (!response.ok) {
        const errText = await response.text()
        throw new Error(`HTTP ${response.status}: ${errText}`)
      }

      await consumeSSEStream(response, assistantMsgId, activeBotId, controller)
    } catch (err: any) {
      setActiveRunsMap((prev) => {
        if (!prev[activeBotId]) return prev
        const next = { ...prev }
        delete next[activeBotId]
        return next
      })
      if (err.name !== 'AbortError') {
        setMessages((prev) =>
          prev.map((msg) =>
            msg.id === assistantMsgId
              ? { ...msg, content: `❌ 请求失败: ${err.message || '网络连接中断'}` }
              : msg
          )
        )
      }
      if (abortControllerRef.current === controller) {
        abortControllerRef.current = null
        setIsGenerating(false)
      }
    }
  }

  const handleStopGeneration = async () => {
    if (abortControllerRef.current) {
      abortControllerRef.current.abort()
      abortControllerRef.current = null
    }
    setIsGenerating(false)
    setActiveRunsMap((prev) => {
      if (!prev[activeBotId]) return prev
      const next = { ...prev }
      delete next[activeBotId]
      return next
    })
    if (activeBotId) {
      try {
        await fetch(`/api/agent/bots/${activeBotId}/stop`, { method: 'POST' })
      } catch (e) {
        console.error('Failed to stop run:', e)
      }
    }
    showToast({ type: 'info', message: '已中断智能体执行' }, 2000)
  }

  const handleClearHistory = async () => {
    if (!confirm('确定清空当前智能体的会话历史吗？记忆和技能将被保留。')) return
    try {
      await fetch(`/api/agent/bots/${activeBotId}/history`, { method: 'DELETE' })
      revokeDetachedPreviewUrls([])
      setMessages([])
      setHasMoreHistory(false)
      setTotalHistoryCount(0)
      showToast({ type: 'success', message: '会话已清空' }, 2000)
    } catch {
      showToast({ type: 'error', message: '清空会话失败' })
    }
  }

  const handleCreateBot = async () => {
    if (!newBotData.name.trim()) {
      showToast({ type: 'warning', message: '请填写智能体名称' })
      return
    }
    if (isCreatingBot) return

    setIsCreatingBot(true)
    setCreateProgressStage(1)

    // 阶段进度展示，提供清晰的智能体构建进度感知
    const timer1 = setTimeout(() => {
      setCreateProgressStage(2)
    }, 700)
    const timer2 = setTimeout(() => {
      setCreateProgressStage(3)
    }, 1500)

    try {
      const payload: any = {
        name: newBotData.name.trim(),
        goal: newBotData.goal.trim(),
        avatar: newBotData.avatar || '🤖',
      }
      if (selectedTemplateId) {
        const tmpl = botTemplates.find((t) => t.id === selectedTemplateId)
        if (tmpl && tmpl.name === newBotData.name.trim()) {
          payload.description = tmpl.description
          payload.base_prompt = tmpl.base_prompt
          payload.suggested_prompts = tmpl.suggested_prompts
        }
      }
      const res = await fetch('/api/agent/bots', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })
      const data = await res.json()
      if (res.ok && data.success) {
        clearTimeout(timer1)
        clearTimeout(timer2)
        setCreateProgressStage(4)
        // 稍作停顿让用户看到完成反馈
        await new Promise((r) => setTimeout(r, 600))
        showToast({ type: 'success', message: `🎉 专属智能体「${data.bot.name}」已创建就绪！` }, 3500)
        setIsCreateBotModalOpen(false)
        setNewBotData({ name: '', goal: '', avatar: '🤖' })
        setUserSelectedAvatar(false)
        setSelectedTemplateId(null)
        setIsCreatingBot(false)
        setCreateProgressStage(0)
        await fetchBots()
        setActiveBotId(data.bot.id)
      } else {
        clearTimeout(timer1)
        clearTimeout(timer2)
        setIsCreatingBot(false)
        setCreateProgressStage(0)
        showToast({ type: 'warning', message: data.detail || '创建失败，请稍后重试' })
      }
    } catch (e: any) {
      clearTimeout(timer1)
      clearTimeout(timer2)
      setIsCreatingBot(false)
      setCreateProgressStage(0)
      showToast({ type: 'warning', message: e.message || '网络异常，请重试' })
    }
  }

  // 触发二次确认删除弹窗
  const handleDeleteActiveBot = () => {
    if (!activeBot || activeBot.id === 'default_assistant') {
      showToast({ type: 'warning', message: '默认助手受系统内置保护，不可删除' })
      return
    }
    setDeleteConfirmInput('')
    setIsDeleteConfirmModalOpen(true)
  }

  // 二次确认执行彻底删除智能体并清理全部关联数据
  const handleConfirmDeleteActiveBot = async () => {
    if (!activeBot || activeBot.id === 'default_assistant') return
    if (deleteConfirmInput.trim() !== activeBot.name.trim()) {
      showToast({ type: 'warning', message: '输入的智能体名称不匹配' })
      return
    }

    setIsDeletingBot(true)
    try {
      const res = await fetch(`/api/agent/bots/${activeBot.id}`, { method: 'DELETE' })
      const data = await res.json()
      if (res.ok && data.success) {
        showToast({ type: 'success', message: `智能体「${activeBot.name}」及其全部关联数据已彻底清理删除` }, 3000)
        setIsDeleteConfirmModalOpen(false)
        setDeleteConfirmInput('')
        setIsDrawerOpen(false)
        await fetchBots()
        setActiveBotId('default_assistant')
      } else {
        showToast({ type: 'error', message: data.detail || '删除智能体失败' })
      }
    } catch (e: any) {
      showToast({ type: 'error', message: e.message || '网络异常' })
    } finally {
      setIsDeletingBot(false)
    }
  }

  // 渲染包含智能体名称/协同的用户发言气泡 (支持自然语言及 @ 智能体)
  const renderUserContentWithMentions = (content: string, allBots: BotSummary[]) => {
    if (!content) return null
    // 过滤出有实际名字的智能体（长度>=2），按名称长度降序排列优先匹配长词
    const botNames = allBots
      .map((b) => b.name)
      .filter((n) => n && n.length >= 2)
      .sort((a, b) => b.length - a.length)

    if (botNames.length === 0) return content

    // 匹配 @智能体名称 或 直接出现的 智能体名称
    const escaped = botNames.map((n) => n.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|')
    const regex = new RegExp(`(@?(?:${escaped}))`, 'g')
    const parts = content.split(regex)

    return parts.map((part, i) => {
      const cleanName = part.startsWith('@') ? part.slice(1) : part
      const matchedBot = allBots.find((b) => b.name === cleanName)
      if (matchedBot && cleanName) {
        return (
          <span
            key={i}
            className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-md font-bold text-blue-900 bg-white/95 dark:bg-slate-900/90 dark:text-blue-300 text-[11px] shadow-2xs mx-0.5 align-baseline"
          >
            <span>{matchedBot.avatar || '🤖'}</span>
            <span>{part.startsWith('@') ? part : `@${matchedBot.name}`}</span>
          </span>
        )
      }
      return <span key={i}>{part}</span>
    })
  }

  // 键盘与输入管理：支持 @ 触发智能体协同下拉选择
  const handleInputChange = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    const val = e.target.value
    setInputMessage(val)

    const cursorPos = e.target.selectionStart || val.length
    const textBeforeCursor = val.slice(0, cursorPos)
    const match = textBeforeCursor.match(/@([^\s@]*)$/)
    if (match) {
      setMentionFilter(match[1])
      setIsMentionDropdownOpen(true)
      setSelectedMentionIndex(0)
    } else {
      setIsMentionDropdownOpen(false)
    }
  }

  const handleInputKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (isMentionDropdownOpen && filteredMentionBots.length > 0) {
      if (e.key === 'ArrowDown') {
        e.preventDefault()
        setSelectedMentionIndex((prev) => (prev + 1) % filteredMentionBots.length)
        return
      }
      if (e.key === 'ArrowUp') {
        e.preventDefault()
        setSelectedMentionIndex((prev) => (prev - 1 + filteredMentionBots.length) % filteredMentionBots.length)
        return
      }
      if (e.key === 'Enter' || e.key === 'Tab') {
        e.preventDefault()
        handleSelectMentionBot(filteredMentionBots[selectedMentionIndex] || filteredMentionBots[0])
        return
      }
      if (e.key === 'Escape') {
        e.preventDefault()
        setIsMentionDropdownOpen(false)
        return
      }
    }

    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleSendMessage()
    }
  }

  const handleSelectMentionBot = (bot: BotSummary) => {
    const textarea = textareaRef.current
    const cursorPos = textarea?.selectionStart ?? inputMessage.length
    const textBeforeCursor = inputMessage.slice(0, cursorPos)
    const textAfterCursor = inputMessage.slice(cursorPos)
    const replacedBefore = textBeforeCursor.replace(/@([^\s@]*)$/, `@${bot.name} `)
    const nextVal = replacedBefore + textAfterCursor
    setInputMessage(nextVal)
    setIsMentionDropdownOpen(false)
    setTimeout(() => {
      if (textarea) {
        textarea.focus()
        const newPos = replacedBefore.length
        textarea.setSelectionRange(newPos, newPos)
      }
    }, 10)
  }

  const handleRefreshSuggestedPrompts = async () => {
    if (!activeBotId || isRefreshingPrompts) return
    setIsRefreshingPrompts(true)
    try {
      const res = await fetch(`/api/agent/bots/${activeBotId}/suggested-prompts`, {
        method: 'POST',
      })
      const data = await res.json()
      if (res.ok && data.success && data.suggested_prompts) {
        setActiveBot((prev: any) => ({
          ...prev,
          suggested_prompts: data.suggested_prompts,
        }))
        showToast({ type: 'success', message: '已根据智能体人设换一批全新推荐问题！' }, 2000)
      } else {
        showToast({ type: 'warning', message: data.detail || '刷新推荐失败' })
      }
    } catch {
      showToast({ type: 'warning', message: '网络异常' })
    } finally {
      setIsRefreshingPrompts(false)
    }
  }

  const handleDeleteGuideline = async (gId: string) => {
    try {
      const res = await fetch(`/api/agent/bots/${activeBotId}/guidelines/${gId}`, { method: 'DELETE' })
      if (res.ok) {
        showToast({ type: 'success', message: '准则已删除' })
        loadBotDetails(activeBotId)
      }
    } catch {
      showToast({ type: 'error', message: '删除失败' })
    }
  }

  const handleDeleteMemory = async (key: string) => {
    try {
      const res = await fetch(`/api/agent/bots/${activeBotId}/memory/${encodeURIComponent(key)}`, {
        method: 'DELETE',
      })
      if (res.ok) {
        showToast({ type: 'success', message: `记忆 [${key}] 已删除` })
        loadBotDetails(activeBotId)
      }
    } catch {
      showToast({ type: 'error', message: '删除失败' })
    }
  }

  const handleSaveBasePrompt = async (newPrompt: string) => {
    if (!activeBotId) return
    if (activeBotId === 'default_assistant') {
      showToast({ type: 'warning', message: '内置智能体基础设定受系统保护，不可修改' })
      return
    }
    setIsSavingBasePrompt(true)
    try {
      const res = await fetch(`/api/agent/bots/${activeBotId}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ base_prompt: newPrompt }),
      })
      const data = await res.json()
      if (res.ok) {
        showToast({ type: 'success', message: '智能体人设与提示词已更新！' })
        if (data.bot) {
          setActiveBot(data.bot)
        } else {
          loadBotDetails(activeBotId)
        }
        fetchBots()
      } else {
        showToast({ type: 'warning', message: data.detail || '保存提示词失败' })
      }
    } catch (e: any) {
      showToast({ type: 'warning', message: e.message || '网络异常' })
    } finally {
      setIsSavingBasePrompt(false)
    }
  }

  const handleOpenEditProfileModal = () => {
    if (!activeBot || activeBot.id === 'default_assistant') {
      showToast({ type: 'info', message: '内置智能体信息受保护，不可编辑' })
      return
    }
    setEditProfileData({
      name: activeBot.name || '',
      avatar: activeBot.avatar || '🤖',
      description: activeBot.description || '',
    })
    setIsEditProfileModalOpen(true)
  }

  const handleSaveProfile = async () => {
    if (!activeBotId || activeBotId === 'default_assistant') return
    if (!editProfileData.name.trim()) {
      showToast({ type: 'warning', message: '智能体名称不能为空' })
      return
    }
    setIsSavingProfile(true)
    try {
      const res = await fetch(`/api/agent/bots/${activeBotId}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: editProfileData.name.trim(),
          avatar: editProfileData.avatar.trim() || '🤖',
          description: editProfileData.description.trim(),
        }),
      })
      const data = await res.json()
      if (res.ok) {
        showToast({ type: 'success', message: '智能体资料已更新！' })
        setIsEditProfileModalOpen(false)
        if (data.bot) {
          setActiveBot(data.bot)
        } else {
          loadBotDetails(activeBotId)
        }
        fetchBots()
      } else {
        showToast({ type: 'warning', message: data.detail || '保存失败' })
      }
    } catch (e: any) {
      showToast({ type: 'warning', message: e.message || '网络异常' })
    } finally {
      setIsSavingProfile(false)
    }
  }
  const handleSaveSkillConfirm = async () => {
    if (!skillToSave.name.trim() || !skillToSave.python_code.trim()) {
      showToast({ type: 'warning', message: '技能名称与脚本代码不能为空' })
      return
    }
    try {
      const res = await fetch(`/api/agent/bots/${activeBotId}/skills`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(skillToSave),
      })
      const data = await res.json()
      if (res.ok) {
        showToast({ type: 'success', message: `⚡ 技能「${skillToSave.name}」已沉淀到当前智能体！` }, 3500)
        setIsSaveSkillModalOpen(false)
        loadBotDetails(activeBotId)
        fetchBots()
        setActiveTab('skills')
        setIsDrawerOpen(true)
      } else {
        showToast({ type: 'warning', message: data.detail || '保存技能失败' })
      }
    } catch (e: any) {
      showToast({ type: 'warning', message: e.message || '网络异常' })
    }
  }

  const handleDeleteSkill = async (skillName: string) => {
    if (!confirm(`确定删除技能「${skillName}」吗？`)) return
    try {
      const res = await fetch(`/api/agent/bots/${activeBotId}/skills/${encodeURIComponent(skillName)}`, {
        method: 'DELETE',
      })
      if (res.ok) {
        showToast({ type: 'success', message: `技能 [${skillName}] 已删除` })
        loadBotDetails(activeBotId)
      }
    } catch {
      showToast({ type: 'error', message: '删除失败' })
    }
  }

  const handleSaveModelConfig = async () => {
    try {
      const res = await fetch('/api/agent/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          ...llmConfig,
          api_key: inputApiKey.trim() || undefined,
        }),
      })
      const data = await res.json()
      if (res.ok) {
        showToast({ type: 'success', message: '大模型配置已保存' }, 3000)
        setIsModelModalOpen(false)
        fetchLlmConfig()
      } else {
        showToast({ type: 'error', message: data.detail || '保存失败' })
      }
    } catch (e: any) {
      showToast({ type: 'error', message: e.message || '网络异常' })
    }
  }

  const handleTestModelConnection = async () => {
    setIsTestingModel(true)
    try {
      const res = await fetch('/api/agent/config/test', { method: 'POST' })
      const data = await res.json()
      if (data.success) {
        showToast({ type: 'success', message: data.message || '模型连接测试成功！' }, 4000)
      } else {
        showToast({ type: 'error', message: data.error || '模型连接失败' }, 5000)
      }
    } catch (e: any) {
      showToast({ type: 'error', message: e.message || '测试连接异常' })
    } finally {
      setIsTestingModel(false)
    }
  }

  const copyToClipboard = (text: string, id: string) => {
    navigator.clipboard.writeText(text)
    setCopiedCodeId(id)
    setTimeout(() => setCopiedCodeId(null), 2000)
  }
  return (
    <div className="flex h-full w-full overflow-hidden bg-slate-50 dark:bg-slate-950 relative">
      {/* 移动端智能体抽屉遮罩 (Backdrop) */}
      {isMobile && isMobileListOpen && (
        <div
          onClick={() => setIsMobileListOpen(false)}
          className="fixed inset-0 bg-slate-900/60 backdrop-blur-xs z-40 lg:hidden animate-in fade-in duration-200"
        />
      )}

      {/* 1. 左侧智能体列表栏 (类似微信/WhatsApp风格的会话抽屉，移动端适配轻巧滑出抽屉) */}
      <aside
        className={`border-r border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900 flex flex-col shrink-0 transition-transform duration-200 ${
          isMobile
            ? `fixed inset-y-0 left-0 z-50 w-72 max-w-[85vw] shadow-2xl ${
                isMobileListOpen ? 'translate-x-0' : '-translate-x-full'
              }`
            : 'w-64 lg:w-72 h-full'
        }`}
      >
        <div className="h-14 shrink-0 px-3.5 border-b border-slate-200 dark:border-slate-800 flex items-center justify-between">
          <div className="flex items-center gap-2">
            <div className="h-7 w-7 rounded-lg bg-blue-600/10 text-blue-600 dark:text-blue-400 flex items-center justify-center">
              <Bot className="h-4 w-4" />
            </div>
            <span className="text-xs font-bold text-slate-800 dark:text-slate-100">智能体工作台</span>
          </div>
          <button
            onClick={() => setIsCreateBotModalOpen(true)}
            className="flex items-center gap-1 px-2.5 py-1 rounded-lg bg-blue-600 text-white hover:bg-blue-500 text-[11px] font-medium transition-all shadow-xs cursor-pointer"
            title="创建专属智能体"
          >
            <Plus className="h-3 w-3" />
            <span>新建</span>
          </button>
        </div>

        {/* 智能体业务域分类微 Tab */}
        <div className="px-2.5 py-1.5 border-b border-slate-100 dark:border-slate-800 bg-slate-50/70 dark:bg-slate-950/40 flex items-center gap-1 shrink-0">
          <button
            type="button"
            onClick={() => setBotDomainFilter('all')}
            className={`flex-1 py-1 rounded-lg text-[10px] font-semibold transition-all cursor-pointer text-center ${
              botDomainFilter === 'all'
                ? 'bg-white dark:bg-slate-800 text-slate-900 dark:text-slate-100 font-bold shadow-2xs border border-slate-200/80 dark:border-slate-700'
                : 'text-slate-500 hover:text-slate-800 dark:hover:text-slate-200'
            }`}
          >
            全部 ({bots.length})
          </button>
          <button
            type="button"
            onClick={() => setBotDomainFilter('current')}
            className={`flex-1 py-1 rounded-lg text-[10px] font-semibold transition-all cursor-pointer text-center ${
              botDomainFilter === 'current'
                ? activeServiceId === 'live'
                  ? 'bg-emerald-50 dark:bg-emerald-950/60 text-emerald-700 dark:text-emerald-300 font-bold shadow-2xs border border-emerald-200 dark:border-emerald-800'
                  : 'bg-blue-50 dark:bg-blue-950/60 text-blue-700 dark:text-blue-300 font-bold shadow-2xs border border-blue-200 dark:border-blue-800'
                : 'text-slate-500 hover:text-slate-800 dark:hover:text-slate-200'
            }`}
          >
            {activeServiceId === 'live' ? '云直播' : 'IM'}
          </button>
          <button
            type="button"
            onClick={() => setBotDomainFilter('general')}
            className={`flex-1 py-1 rounded-lg text-[10px] font-semibold transition-all cursor-pointer text-center ${
              botDomainFilter === 'general'
                ? 'bg-white dark:bg-slate-800 text-slate-900 dark:text-slate-100 font-bold shadow-2xs border border-slate-200/80 dark:border-slate-700'
                : 'text-slate-500 hover:text-slate-800 dark:hover:text-slate-200'
            }`}
          >
            通用
          </button>
        </div>

        {/* 智能体微信/WhatsApp风格列表 */}
        <div className="flex-1 overflow-y-auto p-2 space-y-1">
          {filteredBots.length === 0 ? (
            <div className="py-8 text-center text-xs text-slate-400 dark:text-slate-500">
              当前业务下暂无专属智能体
            </div>
          ) : (
            filteredBots.map((bot) => {
              const isActive = bot.id === activeBotId
              const timeStr = formatChatTime(bot.last_message?.timestamp || bot.updated_at || bot.created_at)
              const isDefault = bot.id === 'default_assistant'
              const activeRun = activeRunsMap[bot.id]
              const isRunning = Boolean(activeRun && activeRun.status === 'running')
              const streamStatus = activeRun?.stream_status || 'thinking'
              const currentStep = activeRun?.current_step || 1
              const domain = getBotDomain(bot)

              return (
                <button
                  key={bot.id}
                  onClick={() => {
                    setActiveBotId(bot.id)
                    if (isMobile) setIsMobileListOpen(false)
                  }}
                  className={`w-full text-left p-2.5 rounded-xl transition-all cursor-pointer flex items-center gap-3 border ${
                    isActive
                      ? 'bg-blue-50/90 dark:bg-blue-950/50 border-blue-200 dark:border-blue-800/80 shadow-xs'
                      : isRunning
                      ? 'bg-blue-50/40 dark:bg-blue-950/30 border-blue-200/60 dark:border-blue-800/50 text-slate-700 dark:text-slate-300 shadow-2xs hover:bg-blue-50/60 dark:hover:bg-blue-950/50'
                      : 'bg-transparent hover:bg-slate-100/80 dark:hover:bg-slate-800/50 border-transparent text-slate-700 dark:text-slate-300'
                  }`}
                >
                  {/* 头像 */}
                  <div className="relative shrink-0">
                    <div
                      className={`w-11 h-11 rounded-2xl flex items-center justify-center text-xl shadow-2xs border transition-all ${
                        isActive
                          ? 'bg-blue-100 dark:bg-blue-900/60 border-blue-200 dark:border-blue-700 scale-102'
                          : 'bg-slate-100 dark:bg-slate-800 border-slate-200 dark:border-slate-700/80'
                      } ${
                        isRunning
                          ? 'ring-2 ring-blue-500/80 dark:ring-blue-400/90 shadow-sm shadow-blue-500/25'
                          : ''
                      }`}
                    >
                      {bot.avatar || '🤖'}
                    </div>
                    {/* 运行中呼吸光效水波纹指示灯 */}
                    {isRunning && (
                      <span className="absolute -top-1 -right-1 flex h-3 w-3 z-10" title="正在执行任务中">
                        <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-blue-400 opacity-75"></span>
                        <span className="relative inline-flex rounded-full h-3 w-3 bg-blue-500 border border-white dark:border-slate-900"></span>
                      </span>
                    )}
                    {isDefault && (
                      <span className="absolute -bottom-1 -right-1 px-1 py-0.2 rounded-full text-[8px] bg-amber-500 text-white font-bold shadow-2xs leading-none z-10">
                        内置
                      </span>
                    )}
                  </div>

                  {/* 会话信息与最新消息 */}
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center justify-between gap-1 mb-1">
                      <div className="flex items-center gap-1.5 min-w-0 flex-1">
                        <span
                          className={`text-xs font-bold truncate ${
                            isActive ? 'text-blue-600 dark:text-blue-400' : 'text-slate-900 dark:text-slate-100'
                          }`}
                        >
                          {bot.name}
                        </span>
                        {domain === 'live' ? (
                          <span className="px-1 py-0.2 rounded text-[8px] font-mono font-bold bg-emerald-50 dark:bg-emerald-950/60 text-emerald-700 dark:text-emerald-400 border border-emerald-200/80 dark:border-emerald-800 shrink-0">
                            CSS
                          </span>
                        ) : domain === 'im' ? (
                          <span className="px-1 py-0.2 rounded text-[8px] font-mono font-bold bg-blue-50 dark:bg-blue-950/60 text-blue-700 dark:text-blue-400 border border-blue-200/80 dark:border-blue-800 shrink-0">
                            IM
                          </span>
                        ) : (
                          <span className="px-1 py-0.2 rounded text-[8px] font-mono font-semibold bg-slate-100 dark:bg-slate-800 text-slate-500 dark:text-slate-400 shrink-0">
                            通用
                          </span>
                        )}
                      </div>
                    {isRunning ? (
                      <span
                        className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-full text-[10px] font-semibold bg-blue-100/90 dark:bg-blue-900/70 text-blue-600 dark:text-blue-300 border border-blue-200 dark:border-blue-700 shrink-0 shadow-2xs animate-pulse"
                        title={`智能体正在运行: ${streamStatus}`}
                      >
                        <Loader2 className="w-2.5 h-2.5 animate-spin text-blue-500 shrink-0" />
                        <span>
                          {streamStatus === 'executing'
                            ? `执行中${currentStep > 1 ? ` #${currentStep}` : ''}`
                            : streamStatus === 'answering'
                            ? '回复中'
                            : '思考中'}
                        </span>
                      </span>
                    ) : (
                      timeStr && (
                        <span className="text-[10px] text-slate-400 shrink-0 font-mono">
                          {timeStr}
                        </span>
                      )
                    )}
                  </div>

                  {isRunning ? (
                    <p className="text-[11px] text-blue-600 dark:text-blue-400 truncate leading-tight flex items-center gap-1 font-medium animate-pulse">
                      <Sparkles className="w-3 h-3 shrink-0 text-blue-500" />
                      <span className="truncate">
                        {streamStatus === 'executing'
                          ? '正在执行 Python / 工具代码...'
                          : streamStatus === 'answering'
                          ? '正在组织并输出回答...'
                          : '正在分析问题并规划思路...'}
                      </span>
                    </p>
                  ) : (
                    <p className="text-[11px] text-slate-500 dark:text-slate-400 truncate leading-tight">
                      {bot.last_message ? (
                        <>
                          <span className="text-slate-400 dark:text-slate-500">
                            {bot.last_message.role === 'user' ? '我: ' : ''}
                          </span>
                          <span>{bot.last_message.content || '...'}</span>
                        </>
                      ) : (
                        <span className="text-slate-400 dark:text-slate-500 italic">
                          {bot.description || '点击开始对话...'}
                        </span>
                      )}
                    </p>
                  )}

                  {/* 资产统计微徽章 */}
                  {(bot.skills_count || bot.guidelines_count || bot.memories_count) ? (
                    <div className="flex items-center gap-2 mt-1 text-[9px] text-slate-400">
                      {bot.skills_count ? <span>⚡{bot.skills_count}</span> : null}
                      {bot.guidelines_count ? <span>🛡️{bot.guidelines_count}</span> : null}
                      {bot.memories_count ? <span>🧠{bot.memories_count}</span> : null}
                    </div>
                  ) : null}
                </div>
              </button>
            )
          }))}
        </div>

        {/* 底部模型接入状态 */}
        <div className="p-3 border-t border-slate-200 dark:border-slate-800 bg-slate-50 dark:bg-slate-900/60">
          <div className="flex items-center justify-between mb-1">
            <span className="text-[10px] font-semibold text-slate-400 uppercase tracking-wider">大模型引擎</span>
            <button
              onClick={() => setIsModelModalOpen(true)}
              className="text-[11px] text-blue-600 dark:text-blue-400 hover:underline flex items-center gap-0.5 cursor-pointer"
            >
              <Settings className="h-3 w-3" />
              <span>配置</span>
            </button>
          </div>
          <div className="flex items-center gap-2 text-xs font-mono text-slate-600 dark:text-slate-300">
            <span className={`h-2 w-2 rounded-full ${llmConfig.has_api_key ? 'bg-emerald-500' : 'bg-amber-500'}`} />
            <span className="truncate">{llmConfig.model_name || 'deepseek-chat'}</span>
          </div>
        </div>
      </aside>

      {/* 2. 中间主区域：会话沙箱与代码执行 */}
      <section className="flex-1 min-w-0 flex flex-col h-full overflow-hidden bg-slate-50 dark:bg-slate-950 relative">
        {/* 会话顶部状态栏 */}
        <div className="h-14 border-b border-slate-200 dark:border-slate-800 bg-white/80 dark:bg-slate-900/80 px-3 sm:px-4 flex items-center justify-between backdrop-blur-sm z-10 shrink-0 min-w-0">
          <div className="flex items-center gap-2 min-w-0 flex-1">
            {isMobile && (
              <button
                type="button"
                onClick={() => setIsMobileListOpen(true)}
                className="lg:hidden flex items-center justify-center h-8 w-8 rounded-lg border border-slate-200 dark:border-slate-750 bg-white dark:bg-slate-800 text-slate-700 dark:text-slate-200 shrink-0 cursor-pointer shadow-2xs hover:border-blue-400"
                title="打开智能体列表"
              >
                <Menu className="h-4 w-4 text-blue-600 dark:text-blue-400" />
              </button>
            )}
            <div
              onClick={() => setIsDrawerOpen(!isDrawerOpen)}
              className="flex items-center gap-2 min-w-0 cursor-pointer group hover:opacity-85 transition-opacity"
              title="点击打开设置与智能体详情"
            >
              <span className="text-xl group-hover:scale-105 transition-transform shrink-0">{activeBot?.avatar || '🤖'}</span>
              <div className="min-w-0">
                <div className="flex items-center gap-1.5 sm:gap-2 flex-wrap">
                  <h2 className="text-xs sm:text-sm font-bold text-slate-900 dark:text-slate-100 group-hover:text-blue-600 dark:group-hover:text-blue-400 transition-colors flex items-center gap-1.5 truncate">
                    <span>{activeBot?.name || '加载中...'}</span>
                    <Settings className="w-3.5 h-3.5 opacity-0 group-hover:opacity-100 transition-opacity text-slate-400 hidden sm:inline" />
                  </h2>
                  {activeBot?.id === 'default_assistant' ? (
                    <Badge variant="secondary" className="font-mono text-[9px] px-1.5 py-0 bg-amber-50 text-amber-700 dark:bg-amber-950 dark:text-amber-300 border border-amber-200 dark:border-amber-800 font-semibold shrink-0">
                      孵化核心
                    </Badge>
                  ) : (
                    <Badge variant="secondary" className="font-mono text-[9px] px-1.5 py-0 bg-blue-50 text-blue-700 dark:bg-blue-950 dark:text-blue-300 border border-blue-200 dark:border-blue-800 shrink-0">
                      CodeAgent
                    </Badge>
                  )}
                  <Badge variant="outline" className="text-[9px] px-1.5 py-0 text-slate-500 border-slate-200 dark:border-slate-800 hidden xl:inline-flex items-center gap-1 font-normal" title="无需输入 @ 符号，直接在对话中使用自然语言（如'让风控官查违规'）即可自动调度协同">
                    <AtSign className="h-2.5 w-2.5 text-blue-500" />
                    <span>支持自然语言协同</span>
                  </Badge>
                </div>
                <p className="text-[10px] text-slate-400 truncate max-w-xs sm:max-w-md">{activeBot?.description || '腾讯云多业务云架构与自动化运维代码编排'}</p>
              </div>
            </div>
          </div>

          <div className="flex items-center gap-1 sm:gap-1.5 shrink-0">
            <Button
              variant="outline"
              size="sm"
              onClick={() => setShowTopologyModal(true)}
              className="h-8 px-2 sm:px-2.5 text-xs font-medium text-slate-700 dark:text-slate-300 hover:text-sky-600 dark:hover:text-sky-400 cursor-pointer"
              title="查看多智能体协同网络拓扑架构与流转视图"
            >
              <Share2 className="h-3.5 w-3.5 sm:mr-1 text-sky-500" />
              <span className="hidden sm:inline">协同拓扑</span>
              <span className="sm:hidden">拓扑</span>
            </Button>

            {/* 导出排障记录报告下拉菜单 */}
            <div className="relative" ref={exportMenuRef}>
              <Button
                variant="outline"
                size="sm"
                onClick={() => setShowExportMenu(!showExportMenu)}
                disabled={isExporting}
                className="h-8 px-2 sm:px-2.5 text-xs font-medium text-slate-700 dark:text-slate-300 hover:text-blue-600 dark:hover:text-blue-400 cursor-pointer"
                title="导出会话排障记录为 Markdown 或独立 HTML 报告"
              >
                {isExporting ? (
                  <Loader2 className="h-3.5 w-3.5 sm:mr-1 text-blue-500 animate-spin" />
                ) : (
                  <Download className="h-3.5 w-3.5 sm:mr-1 text-blue-500" />
                )}
                <span>
                  {isExporting ? (
                    '生成中...'
                  ) : (
                    <>
                      <span className="hidden sm:inline">导出报告</span>
                      <span className="sm:hidden">导出</span>
                    </>
                  )}
                </span>
              </Button>

              {showExportMenu && (
                <div className="absolute right-0 top-full mt-1.5 w-56 bg-white dark:bg-slate-900 rounded-xl border border-slate-200 dark:border-slate-800 shadow-xl p-1.5 z-50 text-xs space-y-1 animate-in fade-in duration-100">
                  <button
                    type="button"
                    onClick={() => handleExportChat('html')}
                    disabled={isExporting}
                    className="w-full flex items-center gap-2.5 px-2.5 py-2 rounded-lg hover:bg-slate-100 dark:hover:bg-slate-800 text-slate-700 dark:text-slate-200 text-left cursor-pointer transition-colors"
                  >
                    <Globe className="h-4 w-4 text-emerald-500 shrink-0" />
                    <div className="flex-1 min-w-0">
                      <div className="font-semibold text-slate-900 dark:text-slate-100 flex items-center justify-between">
                        <span>下载 HTML 报告</span>
                        <span className="text-[10px] text-emerald-600 dark:text-emerald-400 font-mono">.html</span>
                      </div>
                      <div className="text-[10px] text-slate-400 truncate">单文件自包含、支持离线交互与打印</div>
                    </div>
                  </button>

                  <button
                    type="button"
                    onClick={handlePreviewHtml}
                    className="w-full flex items-center gap-2.5 px-2.5 py-2 rounded-lg hover:bg-slate-100 dark:hover:bg-slate-800 text-slate-700 dark:text-slate-200 text-left cursor-pointer transition-colors"
                  >
                    <ExternalLink className="h-4 w-4 text-blue-500 shrink-0" />
                    <div className="flex-1 min-w-0">
                      <div className="font-semibold text-slate-900 dark:text-slate-100">新标签页预览 HTML</div>
                      <div className="text-[10px] text-slate-400 truncate">免下载直接在浏览器查看与快速打印</div>
                    </div>
                  </button>

                  <div className="h-px bg-slate-100 dark:bg-slate-800 my-1" />

                  <button
                    type="button"
                    onClick={() => handleExportChat('markdown')}
                    disabled={isExporting}
                    className="w-full flex items-center gap-2.5 px-2.5 py-2 rounded-lg hover:bg-slate-100 dark:hover:bg-slate-800 text-slate-700 dark:text-slate-200 text-left cursor-pointer transition-colors"
                  >
                    <FileText className="h-4 w-4 text-indigo-500 shrink-0" />
                    <div className="flex-1 min-w-0">
                      <div className="font-semibold text-slate-900 dark:text-slate-100 flex items-center justify-between">
                        <span>下载 Markdown 报告</span>
                        <span className="text-[10px] text-indigo-600 dark:text-indigo-400 font-mono">.md</span>
                      </div>
                      <div className="text-[10px] text-slate-400 truncate">完整思维链与沙箱执行原始代码</div>
                    </div>
                  </button>
                </div>
              )}
            </div>

            <Button
              variant="outline"
              size="sm"
              onClick={handleClearHistory}
              className="h-8 px-2 sm:px-2.5 text-xs text-slate-600 dark:text-slate-300 hover:text-rose-600"
              title="清空聊天记录"
            >
              <History className="h-3.5 w-3.5 sm:mr-1" />
              <span className="hidden sm:inline">清空</span>
            </Button>

            <Button
              variant="outline"
              size="sm"
              onClick={() => setIsDrawerOpen(!isDrawerOpen)}
              className={`h-8 px-2 sm:px-2.5 text-xs font-medium cursor-pointer ${
                isDrawerOpen
                  ? 'bg-blue-50 dark:bg-blue-950/60 text-blue-600 dark:text-blue-400 border-blue-300 dark:border-blue-700'
                  : 'text-slate-700 dark:text-slate-300'
              }`}
              title="智能体设置与资产"
            >
              <Settings className="h-3.5 w-3.5 sm:mr-1 text-slate-500" />
              <span className="hidden sm:inline">设置</span>
              {(activeBot?.guidelines_count || activeBot?.skills_count || activeBot?.memories_count) ? (
                <span className="ml-1 px-1.5 py-0.2 rounded-full text-[9px] bg-blue-600 text-white font-mono">
                  {(activeBot?.guidelines_count || 0) + (activeBot?.skills_count || 0) + (activeBot?.memories_count || 0)}
                </span>
              ) : null}
            </Button>
          </div>
        </div>

        {/* 聊天气泡列表 */}
        <div
          ref={messagesContainerRef}
          onScroll={handleScroll}
          onWheel={handleWheel}
          className="flex-1 overflow-y-auto overflow-x-hidden px-2.5 sm:px-4 py-4 sm:py-5 space-y-6 relative min-w-0"
          style={{ overflowAnchor: 'auto' }}
        >
          {messages.length === 0 ? (
            <div className="h-full flex flex-col items-center justify-center text-center p-6 max-w-xl mx-auto">
              <div className="h-16 w-16 rounded-2xl bg-gradient-to-tr from-blue-600 to-indigo-600 text-white flex items-center justify-center shadow-lg shadow-blue-500/20 mb-4">
                <Bot className="h-8 w-8" />
              </div>
              <h3 className="text-base font-bold text-slate-800 dark:text-slate-100 mb-1">
                欢迎使用 {activeBot?.name || 'TC 架构智能体'}
              </h3>
              <p className="text-xs text-slate-500 dark:text-slate-400 mb-6 leading-relaxed">
                我具备直接调度腾讯云多业务全量接口（动态挂载 {totalApisCount} 项官方接口）、编写与执行 Python 脚本、以及根据您的要求<strong>自我沉淀技能</strong>和<strong>强化提示词</strong>的超能力。
              </p>

              {/* 快捷指令引导 */}
              <div className="w-full text-left">
                <div className="flex items-center justify-between mb-2 select-none">
                  <span className="text-[11px] font-semibold text-slate-500 dark:text-slate-400 uppercase tracking-wider flex items-center gap-1.5">
                    <Sparkles className="h-3.5 w-3.5 text-amber-500" />
                    <span>针对「{activeBot?.name || '当前智能体'}」的人格推荐指令：</span>
                  </span>
                  <button
                    onClick={handleRefreshSuggestedPrompts}
                    disabled={isRefreshingPrompts}
                    className="text-[11px] text-blue-600 dark:text-blue-400 hover:text-blue-700 dark:hover:text-blue-300 flex items-center gap-1 cursor-pointer font-medium disabled:opacity-40 transition-colors"
                    title="点击根据当前智能体人设，通过大模型换一批推荐指令"
                  >
                    <RefreshCw className={`h-3 w-3 ${isRefreshingPrompts ? 'animate-spin' : ''}`} />
                    <span>换一换</span>
                  </button>
                </div>
                <div className="grid grid-cols-1 gap-2">
                  {(activeBot?.suggested_prompts && activeBot.suggested_prompts.length > 0
                    ? activeBot.suggested_prompts
                    : [
                        `结合全量 ${totalApisCount} 项腾讯云接口，为我分析【${activeBot?.name || '当前智能体'}】的核心能力清单`,
                        '编写 Python 脚本批量检查关键管理员账号或活跃直播流在线状态',
                        '扫描当前系统中的全部群组与推流域名并输出基础信息报表',
                        '将我常用的排查参数与测试流 ID/群 ID 记录在你的长期记忆中',
                      ]
                  ).map((prompt: string, idx: number) => (
                    <button
                      key={idx}
                      onClick={() => handleSendMessage(prompt)}
                      className="p-2.5 text-xs text-left rounded-xl bg-white dark:bg-slate-900 border border-slate-200/80 dark:border-slate-800 hover:border-blue-400 dark:hover:border-blue-600 text-slate-700 dark:text-slate-300 hover:text-blue-600 dark:hover:text-blue-400 transition-all shadow-2xs cursor-pointer flex items-center justify-between group"
                    >
                      <span className="truncate">{prompt}</span>
                      <ArrowRight className="h-3 w-3 opacity-0 group-hover:opacity-100 transition-opacity text-blue-500 shrink-0 ml-2" />
                    </button>
                  ))}
                </div>
              </div>
            </div>
          ) : (
            <>
              {/* 顶部会话历史起点与向上翻页加载状态 */}
              {hasMoreHistory ? (
                <div className="flex items-center justify-center py-2 text-[11px] text-slate-400 select-none">
                  {isLoadingMoreHistory ? (
                    <div className="flex items-center gap-1.5 px-3 py-1 rounded-full bg-slate-100 dark:bg-slate-800 text-blue-600 dark:text-blue-400">
                      <Sparkles className="h-3.5 w-3.5 animate-spin" />
                      <span>正在加载更早历史会话...</span>
                    </div>
                  ) : (
                    <button
                      onClick={loadEarlierHistory}
                      className="flex items-center gap-1 px-3 py-1 rounded-full bg-slate-100 hover:bg-slate-200 dark:bg-slate-800 dark:hover:bg-slate-700 text-slate-500 dark:text-slate-400 hover:text-slate-800 dark:hover:text-slate-200 transition-colors cursor-pointer text-[11px]"
                    >
                      <ArrowDown className="h-3 w-3 rotate-180" />
                      <span>向上滑动或点击加载更早历史</span>
                    </button>
                  )}
                </div>
              ) : (
                <div className="flex items-center justify-center gap-2 py-2 text-[11px] text-slate-400/60 select-none">
                  <div className="h-px bg-slate-200 dark:bg-slate-800/80 w-12" />
                  <span>已展示全部历史会话（共 {totalHistoryCount || messages.length} 条记录）</span>
                  <div className="h-px bg-slate-200 dark:bg-slate-800/80 w-12" />
                </div>
              )}

              {messages.map((msg) => {
              const isUser = msg.role === 'user'

              return isUser ? (
                /* 用户消息：无头像、无名字，纯粹极简右侧气泡，移动划过时才展示操作 */
                <div
                  key={msg.id}
                  className="flex flex-col items-end w-full max-w-4xl mx-auto px-2 sm:px-4 group"
                >
                  <div className="max-w-2xl p-3.5 rounded-2xl bg-blue-600 text-white text-xs leading-relaxed shadow-sm text-left select-text whitespace-pre-wrap">
                    {/* 消息附带的文档/图片展示 */}
                    {msg.attachments && msg.attachments.length > 0 && (
                      <div className="mb-2 space-y-1.5 not-prose">
                        {msg.attachments.map((att, attIdx) => (
                          <div
                            key={att.id || attIdx}
                            className="flex items-center gap-2.5 p-2 rounded-xl bg-blue-700/70 border border-blue-400/40 text-white"
                          >
                            {att.data_url || (att.category === 'image' && att.url) ? (
<AuthenticatedImage
                                src={att.data_url || att.url}
                                alt={att.name}
                                onClick={() => setPreviewImage({ url: att.url || att.data_url || '', name: att.name })}
                                className="w-10 h-10 object-cover rounded-lg border border-white/20 shrink-0 bg-black/10 cursor-zoom-in hover:opacity-85 hover:border-white/50 transition-all"
                                title="点击查看原图"
                              />
                            ) : (
                              <div className="w-8 h-8 rounded-lg bg-white/15 flex items-center justify-center shrink-0">
                                {getAttachmentIcon(att.category, att.type)}
                              </div>
                            )}
                            <div className="flex-1 min-w-0">
                              <div className="text-xs font-semibold truncate" title={att.name}>
                                {att.name}
                              </div>
                              <div className="text-[10px] text-blue-200 truncate">
                                {att.type?.toUpperCase()} · {att.size_formatted || `${Math.round(att.size / 1024)} KB`}
                              </div>
                            </div>
                            {att.url && (
                              <a
                                href={att.url}
                                target="_blank"
                                rel="noreferrer"
                                download={att.name}
                                className="p-1 rounded-lg hover:bg-white/20 text-white/80 hover:text-white transition-colors"
                                title="查看或下载文件"
                              >
                                <Download className="h-3.5 w-3.5" />
                              </a>
                            )}
                          </div>
                        ))}
                      </div>
                    )}
                    {renderUserContentWithMentions(msg.content, bots)}
                  </div>

                  {/* ChatGPT 风格 Hover 悬浮操作栏：默认隐藏不占界面，鼠标划过消息时平滑淡入 */}
                  <div className="flex items-center gap-1.5 mt-1 mr-1 text-[11px] text-slate-400 opacity-0 group-hover:opacity-100 transition-opacity duration-150 select-none">
                    {msg.timestamp && (
                      <span className="text-[10px] text-slate-400/70 mr-1 font-mono">
                        {new Date(msg.timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
                      </span>
                    )}
                    <button
                      onClick={() => copyToClipboard(msg.content, `${msg.id}-user-copy`)}
                      className="p-1 rounded-md hover:bg-slate-200/60 dark:hover:bg-slate-800 text-slate-400 hover:text-slate-600 dark:hover:text-slate-200 transition-colors cursor-pointer"
                      title="复制消息内容"
                    >
                      {copiedCodeId === `${msg.id}-user-copy` ? (
                        <Check className="h-3.5 w-3.5 text-emerald-500" />
                      ) : (
                        <Copy className="h-3.5 w-3.5" />
                      )}
                    </button>
                    <button
                      onClick={() => handleEditUserMessage(msg)}
                      className="p-1 rounded-md hover:bg-slate-200/60 dark:hover:bg-slate-800 text-slate-400 hover:text-slate-600 dark:hover:text-slate-200 transition-colors cursor-pointer"
                      title="编辑并重新发送"
                    >
                      <Edit3 className="h-3.5 w-3.5" />
                    </button>
                  </div>
                </div>
              ) : (
                /* 智能体消息：无头像、无名字，全宽优雅自适应阅读流 */
                <div
                  key={msg.id}
                  className="w-full max-w-4xl mx-auto px-2 sm:px-4 space-y-2 text-left min-w-0 group"
                >
                  {(() => {
                    const run = buildAgentRunFromMessage({
                      thought: msg.thought,
                      codeBlocks: msg.codeBlocks,
                      executionLogs: msg.executionLogs,
                      toolCalls: msg.toolCalls,
                      metadata: msg.metadata,
                      orchestrations: msg.orchestrations || msg.metadata?.orchestrations,
                      isStreaming: msg.isStreaming,
                      streamStatus: msg.streamStatus,
                      currentStep: msg.currentStep,
                      elapsedSeconds: msg.elapsedSeconds,
                    })

                    const hasExecutionRun = Boolean(
                      run.planning ||
                        run.steps.length > 0 ||
                        run.verification ||
                        (run.orchestrations && run.orchestrations.length > 0) ||
                        (msg.thought && msg.thought.trim())
                    )

                    return (
                      <>
                        {/* 会话内嵌执行轨迹 (第一级概览 + 第二级步骤 + 第三级技术按需展开) */}
                        {hasExecutionRun && (
                          <AgentExecutionRun
                            run={run}
                            isStreaming={Boolean(msg.isStreaming && msg.streamStatus !== 'answering')}
                            currentStreamingStep={msg.currentStep || 1}
                            streamingStage={msg.streamStatus === 'thinking' ? 'thinking' : 'executing'}
                            onCopyText={copyToClipboard}
                            copiedId={copiedCodeId}
                          />
                        )}

                        {/* 人机协同高危操作安全门禁卡片 (Consent Gate) */}
                        {msg.pendingApproval && (
                          <ConsentGateCard
                            approval={msg.pendingApproval}
                            onDecide={(decision) => handleApprovalDecision(msg.pendingApproval!.approval_id, decision)}
                            isResolving={isResolvingApproval === msg.pendingApproval.approval_id}
                          />
                        )}

                        {/* 最终业务回复正文 */}
                        {(msg.content || msg.streamStatus === 'answering') && (
                          <div className="min-w-0 max-w-full">
                            <div className="p-4 rounded-2xl bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-800 text-xs leading-relaxed text-slate-800 dark:text-slate-100 shadow-xs relative min-w-0 max-w-full">
                              {msg.content ? (
                                <div className="relative">
                                  <MarkdownView content={msg.content} />
                                  {msg.isStreaming && msg.streamStatus === 'answering' && (
                                    <span className="inline-block w-2 h-4 bg-blue-500 dark:bg-blue-400 animate-pulse ml-1 align-middle rounded-xs" />
                                  )}
                                </div>
                              ) : (
                                <div className="flex items-center gap-2 text-slate-400 text-xs py-1">
                                  <Sparkles className="h-3.5 w-3.5 animate-spin text-blue-500" />
                                  <span>正在流式组织最终专业回复...</span>
                                </div>
                              )}

                              {/* 产出物与生成文件一键下载中心 (Artifacts & Output Files Hub) */}
                              {msg.attachments && msg.attachments.length > 0 && (
                                <div className="mt-3.5 pt-3 border-t border-slate-100 dark:border-slate-800/80 not-prose">
                                  <div className="flex items-center justify-between mb-2">
                                    <div className="flex items-center gap-1.5 text-[11px] font-semibold text-slate-700 dark:text-slate-300">
                                      <FolderArchive className="h-3.5 w-3.5 text-blue-600 dark:text-blue-400" />
                                      <span>本次生成产出物 ({msg.attachments.length})</span>
                                    </div>
                                    {msg.attachments.length > 1 && (
                                      <button
                                        type="button"
                                        onClick={handleExportAllFiles}
                                        className="text-[10px] text-blue-600 dark:text-blue-400 hover:underline flex items-center gap-1 cursor-pointer"
                                        title="打包下载全部生成文件为 ZIP"
                                      >
                                        <Archive className="h-3 w-3" />
                                        <span>打包下载全部 (.zip)</span>
                                      </button>
                                    )}
                                  </div>
                                  <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
                                    {msg.attachments.map((att, attIdx) => {
                                      const dlUrl = att.url
                                        ? att.url.includes('?')
                                          ? `${att.url}&download=true`
                                          : `${att.url}?download=true`
                                        : '#'
                                      return (
                                        <div
                                          key={att.id || attIdx}
                                          className="flex items-center justify-between gap-2.5 p-2.5 rounded-xl bg-slate-50/90 dark:bg-slate-950/70 border border-slate-200 dark:border-slate-800 hover:border-blue-300 dark:hover:border-blue-700 transition-all group/att shadow-2xs"
                                        >
                                          <div className="flex items-center gap-2.5 min-w-0 flex-1">
                                            <div className="w-8 h-8 rounded-lg bg-blue-100/70 dark:bg-blue-950/80 border border-blue-200 dark:border-blue-800 flex items-center justify-center shrink-0">
                                              {getAttachmentIcon(att.category, att.type)}
                                            </div>
                                            <div className="min-w-0 flex-1">
                                              <div className="text-xs font-semibold text-slate-800 dark:text-slate-200 truncate" title={att.name}>
                                                {att.name}
                                              </div>
                                              <div className="text-[10px] text-slate-400 flex items-center gap-1.5 mt-0.5 font-mono">
                                                <span>{att.type?.toUpperCase()}</span>
                                                <span>·</span>
                                                <span>{att.size_formatted || `${Math.round(att.size / 1024)} KB`}</span>
                                              </div>
                                            </div>
                                          </div>
                                          <div className="flex items-center gap-1 shrink-0">
                                            {att.category === 'image' && (
                                              <button
                                                type="button"
                                                onClick={() => setPreviewImage({ url: att.url || att.data_url || '', name: att.name })}
                                                className="p-1.5 rounded-lg hover:bg-slate-200 dark:hover:bg-slate-800 text-slate-500 hover:text-blue-600 transition-colors cursor-pointer"
                                                title="预览原图"
                                              >
                                                <Eye className="h-3.5 w-3.5" />
                                              </button>
                                            )}
                                            <a
                                              href={dlUrl}
                                              download={att.name}
                                              target="_blank"
                                              rel="noreferrer"
                                              className="flex items-center gap-1 px-2.5 py-1 rounded-lg bg-blue-600 hover:bg-blue-500 text-white text-[11px] font-medium shadow-xs transition-colors cursor-pointer no-underline"
                                              title="点击立即下载该文件"
                                            >
                                              <Download className="h-3 w-3" />
                                              <span>下载</span>
                                            </a>
                                          </div>
                                        </div>
                                      )
                                    })}
                                  </div>
                                </div>
                              )}
                            </div>

                            {/* ChatGPT 风格底部操作栏：默认隐藏，鼠标移动到消息上才显示，避免UI复杂 */}
                            {!msg.isStreaming && msg.content && (
                              <div className="flex items-center justify-between text-[11px] text-slate-400 select-none mt-1.5 px-1 opacity-0 group-hover:opacity-100 transition-opacity duration-150">
                                <div className="flex items-center gap-1">
                                  <button
                                    onClick={() => copyToClipboard(msg.content, `${msg.id}-answer`)}
                                    className="p-1 rounded-md hover:bg-slate-100 dark:hover:bg-slate-800 text-slate-400 hover:text-slate-700 dark:hover:text-slate-200 transition-colors cursor-pointer"
                                    title="复制回答"
                                  >
                                    {copiedCodeId === `${msg.id}-answer` ? (
                                      <span className="flex items-center gap-1 text-emerald-600 font-medium text-[10px]">
                                        <Check className="h-3.5 w-3.5 text-emerald-500" />
                                        <span>已复制</span>
                                      </span>
                                    ) : (
                                      <Copy className="h-3.5 w-3.5" />
                                    )}
                                  </button>

                                  <button
                                    onClick={() => handleRegenerate(msg.id)}
                                    className="p-1 rounded-md hover:bg-slate-100 dark:hover:bg-slate-800 text-slate-400 hover:text-slate-700 dark:hover:text-slate-200 transition-colors cursor-pointer"
                                    title="重新生成回答"
                                  >
                                    <RotateCcw className="h-3.5 w-3.5" />
                                  </button>
                                </div>

                                <div className="font-mono text-[10px] text-slate-400 flex items-center gap-1.5 flex-wrap">
                                  {msg.metadata?.metrics ? (
                                    <>
                                      <span className="px-1.5 py-0.5 rounded bg-slate-100 dark:bg-slate-800 text-slate-500 dark:text-slate-400">
                                        ⏱️ {msg.metadata.metrics.elapsed_seconds}s
                                      </span>
                                      <span
                                        className="px-1.5 py-0.5 rounded bg-slate-100 dark:bg-slate-800 text-slate-500 dark:text-slate-400 cursor-help"
                                        title={`输入 Token: ${msg.metadata.metrics.input_tokens} · 输出 Token: ${msg.metadata.metrics.output_tokens}`}
                                      >
                                        🔢 {msg.metadata.metrics.total_tokens} tokens
                                      </span>
                                      {msg.metadata.metrics.code_executions > 0 && (
                                        <span className="px-1.5 py-0.5 rounded bg-blue-50 dark:bg-blue-950/60 text-blue-600 dark:text-blue-400">
                                          💻 {msg.metadata.metrics.code_executions}次执行
                                        </span>
                                      )}
                                      <span className="text-slate-400">{msg.metadata.metrics.model || activeBot?.model_override?.model_name || llmConfig.model_name}</span>
                                    </>
                                  ) : (
                                    <>
                                      <span>{activeBot?.model_override?.model_name || llmConfig.model_name || 'deepseek-chat'}</span>
                                      {msg.elapsedSeconds ? <span>· 耗时 {msg.elapsedSeconds}s</span> : null}
                                    </>
                                  )}
                                </div>
                              </div>
                            )}
                          </div>
                        )}
                      </>
                    )
                  })()}
                </div>
              )
            })}
            </>
          )}
          <div ref={messagesEndRef} />

          {/* 浮动“直达最新”气泡：瞬时直达底部，移除迟缓的滚动动画 */}
          {showScrollBottom && (
            <button
              onClick={() => scrollToBottom()}
              className="sticky bottom-2 left-1/2 -translate-x-1/2 z-20 flex items-center gap-1.5 px-3.5 py-1.5 rounded-full bg-blue-600 hover:bg-blue-500 text-white text-xs font-semibold shadow-lg hover:shadow-xl transition-all cursor-pointer animate-in fade-in slide-in-from-bottom-2 duration-150"
            >
              <ArrowDown className="h-3.5 w-3.5" />
              <span>发现新内容，点击直达最新</span>
            </button>
          )}
        </div>

        {/* 底部输入框：去除通栏白色底框与生硬顶边框，改用悬浮自适应现代聊天卡片 */}
        <div className="p-4 pt-1 shrink-0 bg-transparent relative z-10">
          <div className="max-w-4xl mx-auto px-2 sm:px-4 relative">
            {/* @智能体 协同指挥浮动选择面板 */}
            {isMentionDropdownOpen && filteredMentionBots.length > 0 && (
              <div
                ref={mentionDropdownRef}
                className="absolute bottom-full mb-2.5 left-2 sm:left-4 w-80 max-w-[calc(100vw-32px)] bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-800 rounded-2xl shadow-2xl overflow-hidden z-50 p-1.5 animate-in fade-in slide-in-from-bottom-2 duration-150"
              >
                <div className="px-2.5 py-1.5 text-[10px] font-semibold text-slate-400 dark:text-slate-500 flex items-center justify-between border-b border-slate-100 dark:border-slate-800 mb-1">
                  <span className="flex items-center gap-1 text-blue-600 dark:text-blue-400">
                    <AtSign className="h-3 w-3" />
                    <span>协同指挥智能体</span>
                  </span>
                  <span>↑↓ 选择 · Enter 插入</span>
                </div>
                <div className="max-h-48 overflow-y-auto space-y-0.5">
                  {filteredMentionBots.map((bot, idx) => (
                    <button
                      key={bot.id}
                      type="button"
                      onClick={() => handleSelectMentionBot(bot)}
                      onMouseEnter={() => setSelectedMentionIndex(idx)}
                      className={`w-full text-left px-2.5 py-2 rounded-xl flex items-center gap-2.5 transition-colors cursor-pointer ${
                        selectedMentionIndex === idx
                          ? 'bg-blue-50 dark:bg-blue-950/60 text-blue-600 dark:text-blue-400'
                          : 'hover:bg-slate-50 dark:hover:bg-slate-800/60 text-slate-700 dark:text-slate-200'
                      }`}
                    >
                      <span className="text-lg leading-none shrink-0">{bot.avatar || '🤖'}</span>
                      <div className="flex-1 min-w-0">
                        <div className="text-xs font-bold truncate flex items-center gap-1.5">
                          <span>{bot.name}</span>
                          {bot.id === 'default_assistant' && (
                            <span className="text-[9px] px-1.5 py-0.2 rounded-full bg-amber-100 dark:bg-amber-900/40 text-amber-700 dark:text-amber-300 font-normal">
                              孵化核心
                            </span>
                          )}
                        </div>
                        <div className="text-[10px] text-slate-400 dark:text-slate-500 truncate">
                          {bot.description || '协助处理专业 IM 业务需求'}
                        </div>
                      </div>
                    </button>
                  ))}
                </div>
              </div>
            )}

            {/* 待发送附件展示栏 */}
            {attachedFiles.length > 0 && (
              <div className="flex flex-wrap items-center gap-2 mb-2 p-1.5 bg-slate-50 dark:bg-slate-800/60 rounded-xl border border-slate-200 dark:border-slate-700/60">
                {attachedFiles.map((att, idx) => (
                  <div
                    key={att.id || idx}
                    className="flex items-center gap-1.5 px-2.5 py-1 rounded-lg bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-700 text-xs shadow-2xs group"
                  >
                    {att.data_url || (att.category === 'image' && att.url) ? (
                      <AuthenticatedImage
                        src={att.data_url || att.url}
                        alt={att.name}
                        onClick={() => setPreviewImage({ url: att.url || att.data_url || '', name: att.name })}
                        className="w-4 h-4 object-cover rounded-xs cursor-zoom-in hover:opacity-80"
                        title="点击查看原图"
                      />
                    ) : (
                      getAttachmentIcon(att.category, att.type)
                    )}
                    <span className="max-w-[150px] truncate text-slate-700 dark:text-slate-200 font-medium" title={att.name}>
                      {att.name}
                    </span>
                    <span className="text-[10px] text-slate-400">
                      ({att.size_formatted || `${Math.round(att.size / 1024)} KB`})
                    </span>
                    <button
                      type="button"
                      onClick={() => {
                        revokeLocalPreviewUrl(attachedFiles[idx]?.data_url)
                        setAttachedFiles((prev) => prev.filter((_, i) => i !== idx))
                      }}
                      className="text-slate-400 hover:text-rose-500 transition-colors ml-0.5 cursor-pointer"
                      title="移除附件"
                    >
                      <X className="w-3 h-3" />
                    </button>
                  </div>
                ))}
                {isUploadingFile && (
                  <div className="flex items-center gap-1 px-2 py-1 text-xs text-blue-500 font-medium animate-pulse">
                    <Loader2 className="w-3 h-3 animate-spin" />
                    <span>解析上传中...</span>
                  </div>
                )}
              </div>
            )}

            <div
              onDragOver={handleDragOver}
              onDragLeave={handleDragLeave}
              onDrop={handleDrop}
              className={`flex items-end gap-2 bg-white dark:bg-slate-900 rounded-2xl border p-2.5 shadow-sm transition-all ${
                isDraggingOver
                  ? 'border-blue-500 bg-blue-50/20 dark:bg-blue-950/20 ring-2 ring-blue-500/20'
                  : 'border-slate-200/90 dark:border-slate-800 hover:border-slate-300 dark:hover:border-slate-700 focus-within:border-blue-500 dark:focus-within:border-blue-500 focus-within:ring-2 focus-within:ring-blue-500/10'
              }`}
            >
              <textarea
                ref={textareaRef}
                value={inputMessage}
                onChange={handleInputChange}
                onKeyDown={handleInputKeyDown}
                onPaste={handlePaste}
                placeholder={
                  attachedFiles.length > 0
                    ? `已附加 ${attachedFiles.length} 个文件，可直接按 Enter 发送自动解析，或补充具体分析需求...`
                    : `给 ${activeBot?.name || '智能体'} 发送指令，输入 @智能体 协同指挥、上传文件 (DOCX/XLSX/CSV/图片) 或调度 ${totalApisCount} 项云接口... (Enter 发送)`
                }
                rows={2}
                className="flex-1 bg-transparent resize-none border-0 focus:outline-hidden text-xs text-slate-800 dark:text-slate-100 placeholder-slate-400 p-1 leading-relaxed"
              />

              {/* 文件上传触发按钮 */}
              <label
                title="上传文件 (DOCX, XLSX, CSV, TXT, PDF, 图片等)"
                className={`h-8.5 px-2 rounded-xl text-slate-500 hover:text-blue-600 dark:hover:text-blue-400 hover:bg-slate-100 dark:hover:bg-slate-800 transition-colors flex items-center gap-1 text-xs font-medium cursor-pointer shrink-0 ${
                  isUploadingFile ? 'opacity-50 cursor-not-allowed' : ''
                }`}
              >
                {isUploadingFile ? (
                  <Loader2 className="h-3.5 w-3.5 animate-spin text-blue-500" />
                ) : (
                  <Paperclip className="h-3.5 w-3.5 text-blue-500" />
                )}
                <span className="hidden sm:inline text-[11px] font-medium text-slate-600 dark:text-slate-300">
                  {isUploadingFile ? '上传中' : '文件'}
                </span>
                <input
                  ref={fileInputRef}
                  type="file"
                  multiple
                  onChange={handleFileInputChange}
                  disabled={isUploadingFile || isGenerating}
                  className="hidden"
                  accept=".docx,.xlsx,.xls,.csv,.txt,.json,.md,.log,.pdf,image/*"
                />
              </label>

              {/* 快捷输入 @ 协同指挥按钮 */}
              <button
                type="button"
                onClick={() => {
                  const textBefore = inputMessage.endsWith(' ') || !inputMessage ? inputMessage : inputMessage + ' '
                  setInputMessage(textBefore + '@')
                  setMentionFilter('')
                  setIsMentionDropdownOpen(true)
                  setSelectedMentionIndex(0)
                  setTimeout(() => textareaRef.current?.focus(), 10)
                }}
                title="协同指挥其他智能体 (@)"
                className="h-8.5 px-2 rounded-xl text-slate-500 hover:text-blue-600 dark:hover:text-blue-400 hover:bg-slate-100 dark:hover:bg-slate-800 transition-colors flex items-center gap-1 text-xs font-medium cursor-pointer shrink-0"
              >
                <AtSign className="h-3.5 w-3.5 text-blue-500" />
                <span className="hidden sm:inline text-[11px] font-medium text-slate-600 dark:text-slate-300">协同</span>
              </button>

              {isGenerating ? (
                <button
                  type="button"
                  onClick={handleStopGeneration}
                  className="h-8.5 px-3 rounded-xl bg-rose-600 hover:bg-rose-500 text-white flex items-center gap-1.5 text-xs font-semibold shadow-xs cursor-pointer transition-colors"
                >
                  <Square className="h-3.5 w-3.5" />
                  <span>停止</span>
                </button>
              ) : (
                <button
                  type="button"
                  onClick={() => handleSendMessage()}
                  disabled={!inputMessage.trim() && attachedFiles.length === 0}
                  className="h-8.5 w-8.5 rounded-xl bg-blue-600 hover:bg-blue-500 disabled:opacity-40 text-white flex items-center justify-center shadow-xs cursor-pointer transition-transform active:scale-95 shrink-0"
                >
                  <Send className="h-4 w-4" />
                </button>
              )}
            </div>
            <div className="text-center mt-1.5 select-none">
              <span className="text-[10px] text-slate-400 dark:text-slate-500 truncate block max-w-xs sm:max-w-none mx-auto">
                支持上传 DOCX、XLSX、CSV、TXT、PDF 与图片 · 支持 Python 脚本处理、自然语言多 Agent 协同 · Enter 发送
              </span>
            </div>
          </div>
        </div>
      </section>

      {/* 3. 右侧抽屉 / 看板：智能体资产与自我进化 (提示词准则、技能库、记忆、模型) */}
      <AnimatePresence>
        {isDrawerOpen && (
          <>
            {/* 紧凑/移动端屏幕下的背景遮罩 (点击自动关闭，防止挤压会话窗口) */}
            {isCompact && (
              <motion.div
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                exit={{ opacity: 0 }}
                transition={{ duration: 0.15 }}
                onClick={() => setIsDrawerOpen(false)}
                className="fixed inset-0 bg-slate-900/50 backdrop-blur-xs z-40"
              />
            )}
            <motion.aside
              initial={isCompact ? { x: '100%' } : { width: 0, opacity: 0 }}
              animate={isCompact ? { x: 0 } : { width: Math.min(640, windowWidth * 0.4), opacity: 1 }}
              exit={isCompact ? { x: '100%' } : { width: 0, opacity: 0 }}
              transition={isCompact ? { type: 'spring', damping: 25, stiffness: 250 } : { duration: 0.2 }}
              className={`${
                isCompact
                  ? 'fixed inset-y-0 right-0 z-50 w-full sm:w-[580px] lg:w-[680px] max-w-[95vw] shadow-2xl'
                  : 'relative z-20 shrink-0 shadow-xl'
              } border-l border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900 flex flex-col h-full overflow-hidden`}
            >
              {/* 抽屉头部 */}
              <div className="p-3.5 border-b border-slate-200 dark:border-slate-800 flex items-center justify-between bg-slate-50/50 dark:bg-slate-950/50">
                <div className="flex items-center gap-2">
                  <div className="w-6 h-6 rounded-lg bg-blue-500/10 flex items-center justify-center text-blue-600 dark:text-blue-400">
                    <Settings className="h-3.5 w-3.5" />
                  </div>
                  <div>
                    <div className="text-xs font-bold text-slate-800 dark:text-slate-100 flex items-center gap-1.5">
                      <span>智能体设置</span>
                    </div>
                    <div className="text-[10px] text-slate-400">
                      人设设定 · 准则与技能 · 记忆与知识库挂载
                    </div>
                  </div>
                </div>
                <button
                  onClick={() => setIsDrawerOpen(false)}
                  className="text-slate-400 hover:text-slate-600 dark:hover:text-slate-200 p-1 rounded-md cursor-pointer text-base leading-none"
                  title="关闭设置"
                >
                  <X className="h-4 w-4" />
                </button>
              </div>

              {isAdminUser && activeBot && activeBot.visibility !== 'public' && (
                <div className="mx-3 mt-3 rounded-lg border border-amber-200 bg-amber-50 p-2.5 dark:border-amber-800 dark:bg-amber-950/30">
                  <div className="mb-1 text-[11px] font-semibold text-amber-800 dark:text-amber-300">
                    {activeBot.owner_id ? 'BOT 所有权重新转移' : '历史 BOT 所有权迁移'}
                  </div>
                  {activeBot.owner_id && (
                    <div className="mb-1.5 text-[11px] text-amber-700 dark:text-amber-400">
                      当前所有者：<span className="font-mono">{activeBot.owner_id}</span>
                    </div>
                  )}
                  <div className="flex gap-1.5">
                    <input
                      value={ownershipSubject}
                      onChange={(event) => setOwnershipSubject(event.target.value)}
                      placeholder="输入 OIDC subject"
                      className="min-w-0 flex-1 rounded-md border border-amber-200 bg-white px-2 py-1 text-[11px] dark:border-amber-700 dark:bg-slate-900"
                    />
                    <button
                      type="button"
                      onClick={handleTransferOwnership}
                      disabled={!ownershipSubject.trim()}
                      className="rounded-md bg-amber-600 px-2 py-1 text-[11px] font-semibold text-white disabled:opacity-50"
                    >
                      {activeBot.owner_id ? '重新转移' : '转移'}
                    </button>
                  </div>
                </div>
              )}

              {/* Tab 切换栏 (6个 Tab: 人设设定、强化准则、自编技能、长期记忆、文件产出、企业知识库) */}
            <div className="flex items-center gap-1 border-b border-slate-200 dark:border-slate-800 bg-slate-50 dark:bg-slate-950 p-1 text-xs overflow-x-auto scrollbar-none">
              <button
                onClick={() => setActiveTab('prompt')}
                className={`flex-1 min-w-[70px] whitespace-nowrap py-1.5 px-2 rounded-lg font-medium transition-all cursor-pointer flex items-center justify-center gap-1 ${
                  activeTab === 'prompt'
                    ? 'bg-white dark:bg-slate-800 text-blue-600 dark:text-blue-400 shadow-2xs font-bold'
                    : 'text-slate-500 hover:text-slate-700'
                }`}
                title="查看与编辑人设、实时组装提示词与智能体档案"
              >
                <FileText className="h-3 w-3 shrink-0" />
                <span>设定</span>
              </button>

              <button
                onClick={() => setActiveTab('guidelines')}
                className={`flex-1 min-w-[76px] whitespace-nowrap py-1.5 px-2 rounded-lg font-medium transition-all cursor-pointer flex items-center justify-center gap-1 ${
                  activeTab === 'guidelines'
                    ? 'bg-white dark:bg-slate-800 text-blue-600 dark:text-blue-400 shadow-2xs font-bold'
                    : 'text-slate-500 hover:text-slate-700'
                }`}
              >
                <Shield className="h-3 w-3 shrink-0" />
                <span>准则 ({activeBot?.learned_guidelines?.length || 0})</span>
              </button>

              <button
                onClick={() => setActiveTab('skills')}
                className={`flex-1 min-w-[76px] whitespace-nowrap py-1.5 px-2 rounded-lg font-medium transition-all cursor-pointer flex items-center justify-center gap-1 ${
                  activeTab === 'skills'
                    ? 'bg-white dark:bg-slate-800 text-blue-600 dark:text-blue-400 shadow-2xs font-bold'
                    : 'text-slate-500 hover:text-slate-700'
                }`}
              >
                <Zap className="h-3 w-3 shrink-0" />
                <span>技能 ({activeBot?.skills?.length || 0})</span>
              </button>

              <button
                onClick={() => setActiveTab('memory')}
                className={`flex-1 min-w-[76px] whitespace-nowrap py-1.5 px-2 rounded-lg font-medium transition-all cursor-pointer flex items-center justify-center gap-1 ${
                  activeTab === 'memory'
                    ? 'bg-white dark:bg-slate-800 text-blue-600 dark:text-blue-400 shadow-2xs font-bold'
                    : 'text-slate-500 hover:text-slate-700'
                }`}
              >
                <Brain className="h-3 w-3 shrink-0" />
                <span>记忆 ({activeBot?.memories?.length || 0})</span>
              </button>

              <button
                onClick={() => {
                  setActiveTab('files')
                  fetchBotFiles()
                }}
                className={`flex-1 min-w-[76px] whitespace-nowrap py-1.5 px-2 rounded-lg font-medium transition-all cursor-pointer flex items-center justify-center gap-1 ${
                  activeTab === 'files'
                    ? 'bg-white dark:bg-slate-800 text-blue-600 dark:text-blue-400 shadow-2xs font-bold'
                    : 'text-slate-500 hover:text-slate-700'
                }`}
                title="查看与管理该智能体处理过的所有文件与产出物"
              >
                <FolderArchive className="h-3 w-3 shrink-0" />
                <span>文件 ({activeBot?.files_count ?? botFiles.length})</span>
              </button>

              <button
                onClick={() => {
                  setActiveTab('knowledge')
                  fetchKnowledgeDocs()
                }}
                className={`flex-1 min-w-[82px] whitespace-nowrap py-1.5 px-2 rounded-lg font-medium transition-all cursor-pointer flex items-center justify-center gap-1 ${
                  activeTab === 'knowledge'
                    ? 'bg-white dark:bg-slate-800 text-blue-600 dark:text-blue-400 shadow-2xs font-bold'
                    : 'text-slate-500 hover:text-slate-700'
                }`}
                title="挂载业务规范文档与排障手册 (Lightweight RAG)"
              >
                <BookOpen className="h-3 w-3" />
                <span>知识库 ({knowledgeDocs.length})</span>
              </button>
            </div>

            {/* Tab 内容区 */}
            {/* Tab 内容区 */}
            <div className="flex-1 overflow-y-auto p-3 text-xs space-y-3">
              {activeTab === 'prompt' && (
                <AgentPromptTab
                  activeBot={activeBot}
                  llmConfig={llmConfig}
                  botFilesCount={botFiles.length}
                  onSaveBasePrompt={handleSaveBasePrompt}
                  isSavingBasePrompt={isSavingBasePrompt}
                  onOpenEditProfileModal={handleOpenEditProfileModal}
                  onOpenModelModal={() => setIsModelModalOpen(true)}
                  onDeleteActiveBot={handleDeleteActiveBot}
                  onDeleteGuideline={handleDeleteGuideline}
                  onDeleteMemory={handleDeleteMemory}
                  onInspectSkill={(s) => {
                    setInspectingSkill(s)
                    setIsSkillModalOpen(true)
                  }}
                  showToast={showToast}
                />
              )}

              {activeTab === 'guidelines' && (
                <AgentGuidelinesTab
                  guidelines={activeBot?.learned_guidelines || []}
                  onDeleteGuideline={handleDeleteGuideline}
                />
              )}

              {activeTab === 'skills' && (
                <AgentSkillsTab
                  skills={activeBot?.skills || []}
                  onInspectSkill={(s) => {
                    setInspectingSkill(s)
                    setIsSkillModalOpen(true)
                  }}
                  onDeleteSkill={handleDeleteSkill}
                  onRunSkill={(name) => handleSendMessage(`请运行技能 ${name}`)}
                />
              )}

              {activeTab === 'memory' && (
                <AgentMemoryTab
                  memories={activeBot?.memories || []}
                  onDeleteMemory={handleDeleteMemory}
                />
              )}

              {activeTab === 'files' && (
                <FilesTab
                  botFiles={botFiles}
                  isLoadingFiles={isLoadingFiles}
                  isCleaningFiles={isCleaningFiles}
                  onCleanupOrphanFiles={handleCleanupOrphanFiles}
                  onExportAllFiles={handleExportAllFiles}
                  onDeleteBotFile={handleDeleteBotFile}
                  onPreviewImage={setPreviewImage}
                  getAttachmentIcon={getAttachmentIcon}
                />
              )}

              {activeTab === 'knowledge' && (
                <AgentKnowledgeTab
                  activeBotId={activeBotId}
                  knowledgeDocs={knowledgeDocs}
                  isLoadingKnowledge={isLoadingKnowledge}
                  isUploadingKnowledge={isUploadingKnowledge}
                  isSearchingKnowledge={isSearchingKnowledge}
                  knowledgeSearchQuery={knowledgeSearchQuery}
                  setKnowledgeSearchQuery={setKnowledgeSearchQuery}
                  knowledgeSearchResults={knowledgeSearchResults}
                  hasSearchedKnowledge={hasSearchedKnowledge}
                  knowledgeFileInputRef={knowledgeFileInputRef}
                  onUploadKnowledge={handleUploadKnowledge}
                  onToggleKnowledgeDoc={handleToggleKnowledgeDoc}
                  onDeleteKnowledgeDoc={handleDeleteKnowledgeDoc}
                  onTestSearchKnowledge={handleTestSearchKnowledge}
                />
              )}
            </div>
          </motion.aside>
        </>
        )}
      </AnimatePresence>

      {/* 二次确认删除智能体安全弹窗 */}
      {isDeleteConfirmModalOpen && activeBot && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/60 backdrop-blur-xs animate-in fade-in duration-150"
          onClick={() => {
            if (!isDeletingBot) {
              setIsDeleteConfirmModalOpen(false)
              setDeleteConfirmInput('')
            }
          }}
        >
          <div
            className="bg-white dark:bg-slate-900 border border-rose-200 dark:border-rose-900/60 rounded-3xl w-full max-w-md shadow-2xl p-6 space-y-4"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-start gap-3.5 border-b border-slate-100 dark:border-slate-800 pb-4">
              <div className="w-11 h-11 rounded-2xl bg-rose-50 dark:bg-rose-950/60 border border-rose-200/80 dark:border-rose-900 flex items-center justify-center shrink-0">
                <Trash2 className="w-5 h-5 text-rose-600" />
              </div>
              <div>
                <h3 className="text-sm font-bold text-slate-900 dark:text-slate-100">二次确认：彻底删除智能体</h3>
                <p className="text-[11px] text-rose-500 font-semibold mt-0.5">⚠️ 高危不可逆操作：数据将从磁盘彻底抹除</p>
              </div>
            </div>

            <div className="text-xs text-slate-600 dark:text-slate-300 space-y-2 bg-slate-50 dark:bg-slate-950/50 p-3.5 rounded-2xl border border-slate-200/80 dark:border-slate-800/80 leading-relaxed">
              <p className="font-semibold text-slate-800 dark:text-slate-100">
                确定要删除智能体「<span className="text-rose-600 dark:text-rose-400">{activeBot.name}</span>」吗？
              </p>
              <p className="text-[11px] text-slate-500 dark:text-slate-400">
                执行后系统将严格清理并彻底抹除以下全部数据：
              </p>
              <ul className="text-[11px] text-slate-500 dark:text-slate-400 list-disc list-inside space-y-1 pl-1">
                <li>智能体人设身份与配置文件 (<code className="text-slate-600 dark:text-slate-300">config.json</code>)</li>
                <li>该智能体积累的全部长期事实记忆 (<code className="text-slate-600 dark:text-slate-300">memory.json</code>)</li>
                <li>该智能体自主沉淀的全部 Python 专属技能代码 (<code className="text-slate-600 dark:text-slate-300">skills/*.py</code>)</li>
                <li>全部历史对话记录与分析执行轨迹 (<code className="text-slate-600 dark:text-slate-300">history.json</code>)</li>
                <li>立即中断并终止后台运行中的所有会话任务与沙箱</li>
              </ul>
            </div>

            <div className="space-y-1.5">
              <label className="text-[11px] text-slate-600 dark:text-slate-400 font-medium">
                请输入智能体名称「<span className="text-slate-900 dark:text-slate-100 font-bold select-all">{activeBot.name}</span>」以完成二次确认：
              </label>
              <input
                type="text"
                value={deleteConfirmInput}
                onChange={(e) => setDeleteConfirmInput(e.target.value)}
                placeholder={`输入「${activeBot.name}」确认删除`}
                className="w-full text-xs px-3.5 py-2.5 rounded-xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-950 focus:outline-hidden focus:border-rose-500 dark:focus:border-rose-500 focus:ring-2 focus:ring-rose-500/10 text-slate-800 dark:text-slate-100"
                autoFocus
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && deleteConfirmInput.trim() === activeBot.name.trim() && !isDeletingBot) {
                    handleConfirmDeleteActiveBot()
                  }
                }}
              />
            </div>

            <div className="flex justify-end gap-2 pt-2 border-t border-slate-100 dark:border-slate-800">
              <Button
                variant="outline"
                size="sm"
                disabled={isDeletingBot}
                onClick={() => {
                  setIsDeleteConfirmModalOpen(false)
                  setDeleteConfirmInput('')
                }}
                className="text-xs"
              >
                取消
              </Button>
              <Button
                variant="destructive"
                size="sm"
                disabled={deleteConfirmInput.trim() !== activeBot.name.trim() || isDeletingBot}
                onClick={handleConfirmDeleteActiveBot}
                className="bg-rose-600 hover:bg-rose-500 text-white text-xs font-semibold shadow-xs disabled:opacity-40"
              >
                {isDeletingBot ? (
                  <div className="flex items-center gap-1.5">
                    <RefreshCw className="h-3 w-3 animate-spin" />
                    <span>正在彻底清理...</span>
                  </div>
                ) : (
                  '我已了解后果，确认彻底删除'
                )}
              </Button>
            </div>
          </div>
        </div>
      )}

      {/* 4. 新建智能体弹窗（Grok 风格：居中头像选择、自生成 ID 与提示词、进度锁定） */}
      {isCreateBotModalOpen && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/60 backdrop-blur-xs p-4"
          onClick={() => {
            if (!isCreatingBot) {
              setIsCreateBotModalOpen(false)
              setNewBotData({ name: '', goal: '', avatar: '🤖' })
              setUserSelectedAvatar(false)
              setSelectedTemplateId(null)
            }
          }}
        >
          <div
            className="w-full max-w-lg bg-white dark:bg-slate-900 rounded-3xl border border-slate-200 dark:border-slate-800 p-4 sm:p-6 shadow-2xl space-y-4 max-h-[92vh] overflow-y-auto"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center justify-between">
              <h3 className="text-sm font-bold text-slate-900 dark:text-slate-100 flex items-center gap-2">
                <Bot className="h-4 w-4 text-blue-600" />
                <span>创建专属智能体</span>
              </h3>
              {!isCreatingBot && (
                <button
                  onClick={() => {
                    setIsCreateBotModalOpen(false)
                    setNewBotData({ name: '', goal: '', avatar: '🤖' })
                    setUserSelectedAvatar(false)
                    setSelectedTemplateId(null)
                  }}
                  className="text-slate-400 hover:text-slate-600 dark:hover:text-slate-200 cursor-pointer p-1 rounded-lg hover:bg-slate-100 dark:hover:bg-slate-800 transition-colors"
                  title="关闭"
                >
                  <span className="text-lg leading-none">&times;</span>
                </button>
              )}
            </div>

            {isCreatingBot ? (
              <div className="py-4 flex flex-col items-center text-center space-y-5">
                {/* 居中脉冲头像 */}
                <div className="relative">
                  <div className="w-20 h-20 rounded-full bg-linear-to-tr from-blue-500/20 via-indigo-500/20 to-purple-500/20 border-2 border-blue-500/40 flex items-center justify-center text-4xl shadow-lg relative z-10 animate-bounce duration-1000">
                    {newBotData.avatar || '🤖'}
                  </div>
                  <div className="absolute inset-0 rounded-full bg-blue-500/20 blur-xl animate-pulse" />
                </div>

                <div>
                  <h4 className="text-sm font-bold text-slate-900 dark:text-slate-100">
                    正在为您创建专属智能体「{newBotData.name}」
                  </h4>
                  <p className="text-xs text-slate-500 dark:text-slate-400 mt-1">
                    大模型正在自主生成系统人设、编排 REST 接口与沙箱策略
                  </p>
                </div>

                {/* 阶段进度清单 */}
                <div className="w-full bg-slate-50 dark:bg-slate-950/70 rounded-2xl border border-slate-200/80 dark:border-slate-800/80 p-4 space-y-3.5 text-left">
                  {/* Stage 1 */}
                  <div className="flex items-center gap-3">
                    <div className="shrink-0">
                      {createProgressStage > 1 ? (
                        <CheckCircle2 className="w-4 h-4 text-emerald-500" />
                      ) : createProgressStage === 1 ? (
                        <Loader2 className="w-4 h-4 text-blue-500 animate-spin" />
                      ) : (
                        <div className="w-4 h-4 rounded-full border border-slate-300 dark:border-slate-700" />
                      )}
                    </div>
                    <div className="flex-1 min-w-0">
                      <p className="text-xs font-semibold text-slate-800 dark:text-slate-200">
                        角色定位与人设提示词生成
                      </p>
                      <p className="text-[11px] text-slate-400">
                        {createProgressStage > 1 ? '人设定位与思维链逻辑已确立' : '正在分析业务目标并构建 System Prompt...'}
                      </p>
                    </div>
                  </div>

                  {/* Stage 2 */}
                  <div className="flex items-center gap-3">
                    <div className="shrink-0">
                      {createProgressStage > 2 ? (
                        <CheckCircle2 className="w-4 h-4 text-emerald-500" />
                      ) : createProgressStage === 2 ? (
                        <Loader2 className="w-4 h-4 text-blue-500 animate-spin" />
                      ) : (
                        <div className="w-4 h-4 rounded-full border border-slate-300 dark:border-slate-700" />
                      )}
                    </div>
                    <div className="flex-1 min-w-0">
                      <p className="text-xs font-semibold text-slate-800 dark:text-slate-200">
                        腾讯云多业务全量接口权限绑定 ({totalApisCount} APIs)
                      </p>
                      <p className="text-[11px] text-slate-400">
                        {createProgressStage > 2 ? '接口调用链与沙箱权限映射完成' : '正在装配多云 API 工具链与调用准则...'}
                      </p>
                    </div>
                  </div>

                  {/* Stage 3 */}
                  <div className="flex items-center gap-3">
                    <div className="shrink-0">
                      {createProgressStage >= 4 ? (
                        <CheckCircle2 className="w-4 h-4 text-emerald-500" />
                      ) : createProgressStage === 3 ? (
                        <Loader2 className="w-4 h-4 text-blue-500 animate-spin" />
                      ) : (
                        <div className="w-4 h-4 rounded-full border border-slate-300 dark:border-slate-700" />
                      )}
                    </div>
                    <div className="flex-1 min-w-0">
                      <p className="text-xs font-semibold text-slate-800 dark:text-slate-200">
                        专属工作空间与执行引擎注册
                      </p>
                      <p className="text-[11px] text-slate-400">
                        {createProgressStage >= 4 ? '智能体引擎就绪，正在接入控制台...' : '正在初始化记忆体与 Python 执行上下文...'}
                      </p>
                    </div>
                  </div>
                </div>

                {/* 进度条与防关闭提示 */}
                <div className="w-full space-y-2">
                  <div className="h-1.5 w-full bg-slate-100 dark:bg-slate-800 rounded-full overflow-hidden">
                    <div
                      className="h-full bg-linear-to-r from-blue-600 to-indigo-600 transition-all duration-500 rounded-full"
                      style={{
                        width:
                          createProgressStage === 1
                            ? '35%'
                            : createProgressStage === 2
                            ? '70%'
                            : createProgressStage === 3
                            ? '90%'
                            : '100%',
                      }}
                    />
                  </div>
                  <div className="flex items-center justify-center gap-1.5 text-[11px] text-amber-600 dark:text-amber-400 font-medium pt-1">
                    <Lock className="w-3.5 h-3.5" />
                    <span>智能体正在自主构建中，请勿关闭或刷新窗口</span>
                  </div>
                </div>
              </div>
            ) : (
              <div className="space-y-4 text-xs">
                {/* 1. Grok Bot 风格：居中头像与头像选择器 */}
                <div className="flex flex-col items-center text-center">
                  <div className="relative group">
                    <div className="w-20 h-20 rounded-full bg-linear-to-b from-blue-500/10 to-indigo-500/10 dark:from-blue-900/30 dark:to-indigo-900/30 border-2 border-blue-500/30 dark:border-blue-500/40 flex items-center justify-center text-4xl shadow-md transition-all group-hover:scale-105">
                      {newBotData.avatar || '🤖'}
                    </div>
                    <div className="absolute -bottom-1 -right-1 bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-full p-1 shadow-xs text-slate-500">
                      <Sparkles className="w-3.5 h-3.5 text-amber-500" />
                    </div>
                  </div>
                  <p className="text-[11px] text-slate-400 dark:text-slate-500 mt-2 font-medium">选择智能体头像</p>

                  {/* 头像预设快捷选择 */}
                  <div className="flex items-center justify-center gap-1.5 flex-wrap max-w-xs mx-auto mt-2 p-1.5 bg-slate-50 dark:bg-slate-950/60 rounded-2xl border border-slate-200/60 dark:border-slate-800/60">
                    {BOT_AVATAR_PRESETS.map((emoji) => (
                      <button
                        key={emoji}
                        type="button"
                        onClick={() => {
                          setNewBotData((prev) => ({ ...prev, avatar: emoji }))
                          setUserSelectedAvatar(true)
                        }}
                        className={`w-7 h-7 rounded-lg flex items-center justify-center text-base transition-all cursor-pointer ${
                          newBotData.avatar === emoji
                            ? 'bg-white dark:bg-slate-800 ring-2 ring-blue-500 scale-110 shadow-xs'
                            : 'hover:bg-white/80 dark:hover:bg-slate-800/80 opacity-75 hover:opacity-100'
                        }`}
                        title={`选择 ${emoji}`}
                      >
                        {emoji}
                      </button>
                    ))}
                  </div>
                </div>

                {/* 1.5 预设业务角色模板（一键套用） */}
                {botTemplates.length > 0 && (
                  <div className="space-y-1.5 text-left">
                    <div className="flex items-center justify-between">
                      <label className="text-xs font-semibold text-slate-700 dark:text-slate-300 flex items-center gap-1.5">
                        <Sparkles className="w-3.5 h-3.5 text-amber-500" />
                        <span>预设业务角色（一键套用）</span>
                      </label>
                      {selectedTemplateId && (
                        <button
                          type="button"
                          onClick={() => {
                            setSelectedTemplateId(null)
                            setNewBotData({ name: '', goal: '', avatar: '🤖' })
                            setUserSelectedAvatar(false)
                          }}
                          className="text-[11px] text-blue-600 dark:text-blue-400 hover:underline cursor-pointer"
                        >
                          重置表单
                        </button>
                      )}
                    </div>
                    <div className="grid grid-cols-1 sm:grid-cols-3 gap-2">
                      {botTemplates.map((tmpl) => {
                        const isSelected = selectedTemplateId === tmpl.id
                        return (
                          <button
                            key={tmpl.id}
                            type="button"
                            onClick={() => {
                              setSelectedTemplateId(tmpl.id)
                              setNewBotData({
                                name: tmpl.name,
                                goal: tmpl.goal,
                                avatar: tmpl.avatar,
                              })
                              setUserSelectedAvatar(true)
                            }}
                            className={`flex flex-col items-start text-left p-2.5 rounded-2xl border transition-all cursor-pointer relative group ${
                              isSelected
                                ? 'bg-blue-50/90 dark:bg-blue-950/50 border-blue-500 ring-2 ring-blue-500/20 shadow-xs'
                                : 'bg-slate-50/80 dark:bg-slate-950/60 border-slate-200/80 dark:border-slate-800/80 hover:border-blue-400 dark:hover:border-blue-600 hover:bg-slate-100/50'
                            }`}
                          >
                            <div className="flex items-center gap-1.5 w-full">
                              <span className="text-xl leading-none">{tmpl.avatar}</span>
                              <span className="text-xs font-bold text-slate-900 dark:text-slate-100 truncate">
                                {tmpl.name}
                              </span>
                            </div>
                            <p className="text-[11px] text-slate-500 dark:text-slate-400 line-clamp-2 mt-1 leading-tight">
                              {tmpl.description}
                            </p>
                          </button>
                        )
                      })}
                    </div>
                  </div>
                )}

                {/* 2. 智能体名称 */}
                <div className="space-y-1">
                  <label className="block text-xs font-semibold text-slate-700 dark:text-slate-300">
                    智能体名称 <span className="text-rose-500">*</span>
                  </label>
                  <input
                    type="text"
                    value={newBotData.name}
                    onChange={(e) => {
                      const val = e.target.value
                      let avatar = newBotData.avatar
                      if (!userSelectedAvatar) {
                        avatar = inferAvatarFromText(`${val} ${newBotData.goal}`)
                      }
                      setNewBotData({ ...newBotData, name: val, avatar })
                    }}
                    placeholder="如: 社群风控官、IM巡检专家、测试小管家"
                    className="w-full px-3.5 py-2.5 rounded-xl bg-slate-50 dark:bg-slate-950 border border-slate-200 dark:border-slate-800 text-xs font-medium focus:border-blue-500 focus:outline-hidden"
                  />
                </div>

                {/* 3. 职责与业务目标 */}
                <div className="space-y-1">
                  <label className="block text-xs font-semibold text-slate-700 dark:text-slate-300">
                    职责与业务目标（希望它做什么）
                  </label>
                  <textarea
                    value={newBotData.goal}
                    onChange={(e) => {
                      const val = e.target.value
                      let avatar = newBotData.avatar
                      if (!userSelectedAvatar) {
                        avatar = inferAvatarFromText(`${newBotData.name} ${val}`)
                      }
                      setNewBotData({ ...newBotData, goal: val, avatar })
                    }}
                    placeholder="简要描述您希望它解决的场景，例如：'负责群聊发言巡检，发现涉赌、违规刷屏时自动禁言并汇总告警'..."
                    rows={3}
                    className="w-full px-3.5 py-2.5 rounded-xl bg-slate-50 dark:bg-slate-950 border border-slate-200 dark:border-slate-800 text-xs leading-relaxed focus:border-blue-500 focus:outline-hidden resize-none"
                  />
                  <p className="text-[11px] text-slate-400 dark:text-slate-500 pt-0.5">
                    💡 系统将基于业务目标自动生成专业人设、分配英文唯一标识并打通腾讯云多业务接口权限。
                  </p>
                </div>

                {/* 4. 底部操作栏 */}
                <div className="flex justify-end gap-2 pt-3 border-t border-slate-100 dark:border-slate-800">
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => {
                      setIsCreateBotModalOpen(false)
                      setNewBotData({ name: '', goal: '', avatar: '🤖' })
                      setUserSelectedAvatar(false)
                      setSelectedTemplateId(null)
                    }}
                  >
                    取消
                  </Button>
                  <Button
                    size="sm"
                    onClick={handleCreateBot}
                    disabled={!newBotData.name.trim()}
                    className="bg-linear-to-r from-blue-600 to-indigo-600 hover:from-blue-500 hover:to-indigo-500 text-white font-semibold shadow-xs disabled:opacity-40"
                  >
                    🚀 一键智能创建
                  </Button>
                </div>
              </div>
            )}
          </div>
        </div>
      )}

      {/* 5. 查看自编技能代码弹窗 */}
      {isSkillModalOpen && inspectingSkill && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/60 backdrop-blur-xs p-4">
          <div className="w-full max-w-2xl bg-white dark:bg-slate-900 rounded-2xl border border-slate-200 dark:border-slate-800 p-4 sm:p-5 shadow-2xl space-y-4 max-h-[92vh] overflow-y-auto">
            <div className="flex items-center justify-between">
              <div>
                <h3 className="text-sm font-bold font-mono text-blue-600 dark:text-blue-400">
                  {inspectingSkill.name}
                </h3>
                <p className="text-xs text-slate-500">{inspectingSkill.description}</p>
              </div>
              <button onClick={() => setIsSkillModalOpen(false)} className="text-slate-400 hover:text-slate-600 cursor-pointer">
                &times;
              </button>
            </div>

            <div className="rounded-xl bg-white dark:bg-slate-950 border border-slate-200 dark:border-slate-800 p-3 text-xs font-mono text-slate-800 dark:text-slate-200 max-h-96 overflow-y-auto">
              <MarkdownView content={`\`\`\`python\n${inspectingSkill.code}\n\`\`\``} />
            </div>

            <div className="flex justify-between items-center pt-2">
              <div className="flex items-center gap-3">
                <span className="text-[11px] text-slate-400 font-mono">创建于: {inspectingSkill.created_at?.slice(0, 10)}</span>
                <button
                  onClick={() => {
                    navigator.clipboard.writeText(inspectingSkill.code)
                    showToast({ type: 'success', message: '已复制代码到剪贴板' }, 2000)
                  }}
                  className="text-[11px] text-slate-400 hover:text-slate-200 flex items-center gap-1 cursor-pointer"
                >
                  <Copy className="h-3 w-3" />
                  <span>复制代码</span>
                </button>
              </div>
              <Button
                size="sm"
                onClick={() => {
                  setIsSkillModalOpen(false)
                  handleSendMessage(`运行技能 ${inspectingSkill.name}`)
                }}
                className="bg-blue-600 hover:bg-blue-500 text-white cursor-pointer"
              >
                在聊天中运行 ▶
              </Button>
            </div>
          </div>
        </div>
      )}

      {/* 6. 大模型配置弹窗 */}
      {isModelModalOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/60 backdrop-blur-xs p-4">
          <div className="w-full max-w-lg max-h-[92vh] overflow-y-auto bg-white dark:bg-slate-900 rounded-2xl border border-slate-200 dark:border-slate-800 p-5 shadow-2xl space-y-4">
            <div className="flex items-center justify-between">
              <h3 className="text-sm font-bold text-slate-900 dark:text-slate-100 flex items-center gap-2">
                <Settings className="h-4 w-4 text-blue-600" />
                <span>大模型连接配置</span>
              </h3>
              <button onClick={() => setIsModelModalOpen(false)} className="text-slate-400 hover:text-slate-600 cursor-pointer">
                &times;
              </button>
            </div>

            <div className="space-y-3 text-xs">
              <div>
                <label className="block text-slate-600 dark:text-slate-400 mb-1 font-medium">模型提供商</label>
                <select
                  value={llmConfig.provider}
                  onChange={(e) => {
                    const p = e.target.value
                    let updated: LLMConfigData
                    if (p === 'deepseek') {
                      updated = { ...llmConfig, provider: p, model_name: 'deepseek-chat', base_url: 'https://api.deepseek.com', context_budget: 32000, max_context_turns: 25 }
                    } else if (p === 'qwen') {
                      updated = { ...llmConfig, provider: p, model_name: 'qwen-plus', base_url: 'https://dashscope.aliyuncs.com/compatible-mode/v1', context_budget: 32000, max_context_turns: 25 }
                    } else if (p === 'ollama') {
                      updated = { ...llmConfig, provider: p, model_name: 'qwen2.5-coder:7b', base_url: 'http://localhost:11434/v1', context_budget: 16000, max_context_turns: 15 }
                    } else if (p === 'openai') {
                      updated = { ...llmConfig, provider: p, model_name: 'gpt-4o', base_url: 'https://api.openai.com/v1', context_budget: 48000, max_context_turns: 30 }
                    } else {
                      updated = { ...llmConfig, provider: p }
                    }
                    setLlmConfig(updated)
                    fetchAvailableModels(false, updated)
                  }}
                  className="w-full px-3 py-2 rounded-xl bg-slate-50 dark:bg-slate-950 border border-slate-200 dark:border-slate-800 text-xs"
                >
                  <option value="deepseek">DeepSeek (推荐: 高性价比、代码与编排强劲)</option>
                  <option value="qwen">通义千问 (DashScope / 阿里百炼)</option>
                  <option value="ollama">Ollama (本地内网运行，0 成本)</option>
                  <option value="openai">OpenAI (GPT-4o)</option>
                  <option value="custom">自定义 OpenAI 兼容接口 (vLLM / FastChat)</option>
                </select>
              </div>

              <div>
                <div className="flex items-center justify-between mb-1">
                  <div className="flex items-center gap-1.5 min-w-0">
                    <label className="text-slate-600 dark:text-slate-400 font-medium">模型标识 (Model ID)</label>
                    {modelFetchMsg && (
                      <span className={`text-[10px] truncate ${modelFetchMsg.type === 'success' ? 'text-emerald-600 dark:text-emerald-400 font-medium' : 'text-amber-500'}`}>
                        · {modelFetchMsg.text}
                      </span>
                    )}
                  </div>
                  <button
                    type="button"
                    onClick={() => fetchAvailableModels(true)}
                    disabled={isFetchingModels}
                    className="text-[11px] text-blue-600 dark:text-blue-400 hover:underline flex items-center gap-1 font-medium cursor-pointer shrink-0 ml-2"
                    title="请求该提供商的 /models 接口在线获取可用模型"
                  >
                    <RefreshCw className={`h-3 w-3 ${isFetchingModels ? 'animate-spin text-blue-500' : ''}`} />
                    <span>{isFetchingModels ? '探测中...' : '通过 /models 在线拉取'}</span>
                  </button>
                </div>

                <div className="relative">
                  <input
                    type="text"
                    list="available-models-datalist"
                    value={llmConfig.model_name}
                    onChange={(e) => setLlmConfig({ ...llmConfig, model_name: e.target.value })}
                    placeholder="输入或选择模型标识，如 deepseek-chat, qwen-plus..."
                    className="w-full px-3 py-2 rounded-xl bg-slate-50 dark:bg-slate-950 border border-slate-200 dark:border-slate-800 text-xs font-mono focus:border-blue-500 outline-none"
                  />
                  {availableModels.length > 0 && (
                    <datalist id="available-models-datalist">
                      {availableModels.map((m) => (
                        <option key={m} value={m} />
                      ))}
                    </datalist>
                  )}
                </div>

                {/* 候选模型快捷点选标签 (保持完全自定义，亦可一键点选切换) */}
                {availableModels.length > 0 && (
                  <div className="mt-2 space-y-1">
                    <div className="flex items-center justify-between text-[10px] text-slate-400">
                      <span>可用模型推荐 (点击一键填入，亦可自由输入修改)：</span>
                      <span className="font-mono text-slate-500">{availableModels.length} 个候选</span>
                    </div>
                    <div className="flex flex-wrap gap-1.5 max-h-24 overflow-y-auto p-1.5 rounded-xl bg-slate-50 dark:bg-slate-950/70 border border-slate-200/70 dark:border-slate-800">
                      {availableModels.map((m) => {
                        const isSelected = llmConfig.model_name === m
                        return (
                          <button
                            key={m}
                            type="button"
                            onClick={() => setLlmConfig({ ...llmConfig, model_name: m })}
                            className={`px-2 py-0.5 rounded-lg text-[10px] font-mono transition-all cursor-pointer border ${
                              isSelected
                                ? 'bg-blue-600 text-white border-blue-600 font-bold shadow-2xs'
                                : 'bg-white dark:bg-slate-850 text-slate-700 dark:text-slate-300 hover:border-blue-400 dark:hover:border-blue-500 hover:text-blue-600 dark:hover:text-blue-400 border-slate-200 dark:border-slate-750'
                            }`}
                          >
                            {m}
                          </button>
                        )
                      })}
                    </div>
                  </div>
                )}
              </div>

              <div>
                <label className="block text-slate-600 dark:text-slate-400 mb-1 font-medium">Base URL</label>
                <input
                  type="text"
                  value={llmConfig.base_url}
                  onChange={(e) => setLlmConfig({ ...llmConfig, base_url: e.target.value })}
                  placeholder="https://api.deepseek.com"
                  className="w-full px-3 py-2 rounded-xl bg-slate-50 dark:bg-slate-950 border border-slate-200 dark:border-slate-800 text-xs font-mono"
                />
              </div>

              <div>
                <label className="block text-slate-600 dark:text-slate-400 mb-1 font-medium">
                  API Key
                  {llmConfig.api_key_masked && (
                    <span className="ml-2 text-[10px] text-emerald-500 font-mono">已配置: {llmConfig.api_key_masked}</span>
                  )}
                </label>
                <input
                  type="password"
                  value={inputApiKey}
                  onChange={(e) => setInputApiKey(e.target.value)}
                  placeholder={llmConfig.has_api_key ? '留空保持原有 Key，或输入新 Key' : '请输入 API Key'}
                  className="w-full px-3 py-2 rounded-xl bg-slate-50 dark:bg-slate-950 border border-slate-200 dark:border-slate-800 text-xs font-mono"
                />
              </div>

              <div className="grid grid-cols-2 gap-3 pt-1">
                <div>
                  <label className="block text-slate-600 dark:text-slate-400 mb-1 font-medium">
                    执行轮数上限 (Max Steps)
                  </label>
                  <input
                    type="number"
                    min={5}
                    max={100}
                    value={llmConfig.max_steps ?? 30}
                    onChange={(e) => setLlmConfig({ ...llmConfig, max_steps: parseInt(e.target.value) || 30 })}
                    placeholder="30"
                    className="w-full px-3 py-2 rounded-xl bg-slate-50 dark:bg-slate-950 border border-slate-200 dark:border-slate-800 text-xs font-mono"
                  />
                  <p className="text-[10px] text-slate-400 mt-0.5">默认 30 轮（支持 10~100 轮）</p>
                </div>
                <div>
                  <label className="block text-slate-600 dark:text-slate-400 mb-1 font-medium">
                    模型温度 (Temperature)
                  </label>
                  <input
                    type="number"
                    step={0.1}
                    min={0}
                    max={2}
                    value={llmConfig.temperature ?? 0.2}
                    onChange={(e) => setLlmConfig({ ...llmConfig, temperature: parseFloat(e.target.value) || 0.2 })}
                    placeholder="0.2"
                    className="w-full px-3 py-2 rounded-xl bg-slate-50 dark:bg-slate-950 border border-slate-200 dark:border-slate-800 text-xs font-mono"
                  />
                  <p className="text-[10px] text-slate-400 mt-0.5">推荐 0.1 ~ 0.3</p>
                </div>
              </div>

              {/* 四层分级架构：即时工作记忆上下文预算 (Context Budgeting) */}
              <div className="pt-2 border-t border-slate-100 dark:border-slate-800 space-y-2">
                <div className="flex items-center justify-between">
                  <label className="text-slate-600 dark:text-slate-400 font-medium flex items-center gap-1.5">
                    <span>上下文工作区预算 (Context Budget)</span>
                  </label>
                  <span className="text-[10px] text-slate-400 font-mono">
                    约 {Math.round((llmConfig.context_budget ?? 32000) / 2)} Tokens / {llmConfig.max_context_turns ?? 25} 轮
                  </span>
                </div>

                {/* 快捷档位预设 */}
                <div className="grid grid-cols-4 gap-1.5">
                  {[
                    { label: '精简省流', chars: 16000, turns: 15, hint: '适合本地 Ollama / 低显存' },
                    { label: '标准均衡', chars: 32000, turns: 25, hint: '推荐 DeepSeek / Qwen 黄金比' },
                    { label: '超长深度', chars: 64000, turns: 35, hint: '适合长周期复杂对账' },
                    { label: '海量吞吐', chars: 128000, turns: 50, hint: '适合 1M+ 超长上下文模型' },
                  ].map((p) => {
                    const active = (llmConfig.context_budget ?? 32000) === p.chars
                    return (
                      <button
                        key={p.chars}
                        type="button"
                        onClick={() =>
                          setLlmConfig({
                            ...llmConfig,
                            context_budget: p.chars,
                            max_context_turns: p.turns,
                          })
                        }
                        title={p.hint}
                        className={`px-2 py-1.5 rounded-lg text-center transition-all cursor-pointer border ${
                          active
                            ? 'bg-blue-600 text-white border-blue-600 font-bold shadow-2xs'
                            : 'bg-slate-50 dark:bg-slate-950 text-slate-600 dark:text-slate-300 border-slate-200 dark:border-slate-800 hover:border-blue-400'
                        }`}
                      >
                        <div className="text-[11px] font-medium leading-tight">{p.label}</div>
                        <div className={`text-[9px] mt-0.5 ${active ? 'text-blue-100' : 'text-slate-400'}`}>
                          {p.chars / 1000}k 字符
                        </div>
                      </button>
                    )
                  })}
                </div>

                <div className="grid grid-cols-2 gap-3 pt-1">
                  <div>
                    <label className="block text-slate-500 dark:text-slate-400 mb-0.5 text-[11px]">
                      字符预算 (Char Budget)
                    </label>
                    <input
                      type="number"
                      step={4000}
                      min={4000}
                      max={256000}
                      value={llmConfig.context_budget ?? 32000}
                      onChange={(e) =>
                        setLlmConfig({ ...llmConfig, context_budget: parseInt(e.target.value) || 32000 })
                      }
                      placeholder="32000"
                      className="w-full px-2.5 py-1.5 rounded-xl bg-slate-50 dark:bg-slate-950 border border-slate-200 dark:border-slate-800 text-xs font-mono"
                    />
                  </div>
                  <div>
                    <label className="block text-slate-500 dark:text-slate-400 mb-0.5 text-[11px]">
                      高保真保留轮数 (Max Turns)
                    </label>
                    <input
                      type="number"
                      min={5}
                      max={100}
                      value={llmConfig.max_context_turns ?? 25}
                      onChange={(e) =>
                        setLlmConfig({ ...llmConfig, max_context_turns: parseInt(e.target.value) || 25 })
                      }
                      placeholder="25"
                      className="w-full px-2.5 py-1.5 rounded-xl bg-slate-50 dark:bg-slate-950 border border-slate-200 dark:border-slate-800 text-xs font-mono"
                    />
                  </div>
                </div>
              </div>
            </div>

            <div className="flex items-center justify-between pt-2">
              <Button
                variant="outline"
                size="sm"
                onClick={handleTestModelConnection}
                disabled={isTestingModel}
                className="text-xs text-slate-600 dark:text-slate-300 cursor-pointer"
              >
                {isTestingModel ? <RefreshCw className="h-3 w-3 animate-spin mr-1" /> : <Server className="h-3 w-3 mr-1" />}
                <span>测试连通性</span>
              </Button>

              <div className="flex gap-2">
                <Button variant="outline" size="sm" onClick={() => setIsModelModalOpen(false)}>
                  取消
                </Button>
                <Button size="sm" onClick={handleSaveModelConfig} className="bg-blue-600 hover:bg-blue-500 text-white">
                  保存配置
                </Button>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* 6.5 编辑智能体资料弹窗 (名称、头像、描述) */}
      {isEditProfileModalOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/60 backdrop-blur-xs p-4">
          <div className="w-full max-w-md bg-white dark:bg-slate-900 rounded-2xl border border-slate-200 dark:border-slate-800 p-4 sm:p-5 shadow-2xl space-y-4 max-h-[92vh] overflow-y-auto">
            <div className="flex items-center justify-between border-b border-slate-100 dark:border-slate-800 pb-3">
              <h3 className="text-sm font-bold text-slate-900 dark:text-slate-100 flex items-center gap-2">
                <Edit3 className="h-4 w-4 text-blue-500" />
                <span>编辑智能体资料</span>
              </h3>
              <button
                onClick={() => setIsEditProfileModalOpen(false)}
                className="text-slate-400 hover:text-slate-600 dark:hover:text-slate-200 cursor-pointer text-lg leading-none"
              >
                &times;
              </button>
            </div>

            <div className="space-y-3.5 text-xs">
              <div>
                <label className="block text-slate-600 dark:text-slate-400 mb-1.5 font-medium">选择或输入头像 (Emoji)</label>
                <div className="flex items-center gap-2 mb-2">
                  <div className="w-10 h-10 rounded-xl bg-blue-50 dark:bg-blue-950/60 border border-blue-200 dark:border-blue-800 flex items-center justify-center text-xl shrink-0">
                    {editProfileData.avatar || '🤖'}
                  </div>
                  <input
                    type="text"
                    value={editProfileData.avatar}
                    onChange={(e) => setEditProfileData({ ...editProfileData, avatar: e.target.value })}
                    placeholder="Emoji"
                    maxLength={4}
                    className="w-20 px-3 py-2 rounded-xl bg-slate-50 dark:bg-slate-950 border border-slate-200 dark:border-slate-800 text-xs text-center font-mono"
                  />
                  <span className="text-[11px] text-slate-400">点击下方快速预设：</span>
                </div>
                <div className="flex flex-wrap gap-1.5 p-2 rounded-xl bg-slate-50 dark:bg-slate-950 border border-slate-200 dark:border-slate-800">
                  {AVATAR_PRESETS.map((emoji) => (
                    <button
                      key={emoji}
                      type="button"
                      onClick={() => setEditProfileData({ ...editProfileData, avatar: emoji })}
                      className={`w-7 h-7 rounded-lg text-sm flex items-center justify-center transition-all cursor-pointer ${
                        editProfileData.avatar === emoji
                          ? 'bg-blue-100 dark:bg-blue-900 border border-blue-400 scale-110 shadow-2xs'
                          : 'hover:bg-white dark:hover:bg-slate-800'
                      }`}
                    >
                      {emoji}
                    </button>
                  ))}
                </div>
              </div>

              <div>
                <label className="block text-slate-600 dark:text-slate-400 mb-1 font-medium">智能体名称</label>
                <input
                  type="text"
                  value={editProfileData.name}
                  onChange={(e) => setEditProfileData({ ...editProfileData, name: e.target.value })}
                  placeholder="如: 群组风控助手"
                  className="w-full px-3 py-2 rounded-xl bg-slate-50 dark:bg-slate-950 border border-slate-200 dark:border-slate-800 text-xs font-medium"
                />
              </div>

              <div>
                <label className="block text-slate-600 dark:text-slate-400 mb-1 font-medium">业务职责与定位描述</label>
                <textarea
                  value={editProfileData.description}
                  onChange={(e) => setEditProfileData({ ...editProfileData, description: e.target.value })}
                  placeholder="简要描述该智能体的专属场景和分工..."
                  rows={3}
                  className="w-full px-3 py-2 rounded-xl bg-slate-50 dark:bg-slate-950 border border-slate-200 dark:border-slate-800 text-xs resize-none"
                />
              </div>
            </div>

            <div className="flex items-center justify-end gap-2 pt-2 border-t border-slate-100 dark:border-slate-800">
              <Button
                variant="outline"
                size="sm"
                onClick={() => setIsEditProfileModalOpen(false)}
                disabled={isSavingProfile}
                className="cursor-pointer"
              >
                取消
              </Button>
              <Button
                size="sm"
                onClick={handleSaveProfile}
                disabled={isSavingProfile || !editProfileData.name.trim()}
                className="bg-blue-600 hover:bg-blue-500 text-white font-semibold cursor-pointer"
              >
                {isSavingProfile ? '保存中...' : '保存资料'}
              </Button>
            </div>
          </div>
        </div>
      )}

      {/* 7. 一键确认沉淀为技能弹窗 */}
      {isSaveSkillModalOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/60 backdrop-blur-xs p-4">
          <div className="w-full max-w-xl bg-white dark:bg-slate-900 rounded-2xl border border-slate-200 dark:border-slate-800 p-4 sm:p-5 shadow-2xl space-y-4 max-h-[92vh] overflow-y-auto">
            <div className="flex items-center justify-between">
              <h3 className="text-sm font-bold text-slate-900 dark:text-slate-100 flex items-center gap-2">
                <Zap className="h-4 w-4 text-amber-500" />
                <span>沉淀为当前智能体专属技能</span>
              </h3>
              <button onClick={() => setIsSaveSkillModalOpen(false)} className="text-slate-400 hover:text-slate-600 cursor-pointer">
                &times;
              </button>
            </div>

            <p className="text-xs text-slate-500 leading-relaxed">
              沉淀为技能后，该操作将固化在当前智能体的专属技能树中。后续您只需吩咐技能名即可一键执行。（即便不沉淀，智能体的实时代码编写与任务执行能力也完全不受任何影响）
            </p>

            <div className="space-y-3 text-xs">
              <div>
                <label className="block text-slate-600 dark:text-slate-400 mb-1 font-medium">技能英文标识</label>
                <input
                  type="text"
                  value={skillToSave.name}
                  onChange={(e) => setSkillToSave({ ...skillToSave, name: e.target.value })}
                  placeholder="如: batch_mute_spammers"
                  className="w-full px-3 py-2 rounded-xl bg-slate-50 dark:bg-slate-950 border border-slate-200 dark:border-slate-800 text-xs font-mono"
                />
              </div>

              <div>
                <label className="block text-slate-600 dark:text-slate-400 mb-1 font-medium">技能功能描述</label>
                <input
                  type="text"
                  value={skillToSave.description}
                  onChange={(e) => setSkillToSave({ ...skillToSave, description: e.target.value })}
                  placeholder="简述该技能解决的业务场景"
                  className="w-full px-3 py-2 rounded-xl bg-slate-50 dark:bg-slate-950 border border-slate-200 dark:border-slate-800 text-xs"
                />
              </div>

              <div>
                <label className="block text-slate-600 dark:text-slate-400 mb-1 font-medium">技能执行代码 (Python)</label>
                <textarea
                  value={skillToSave.python_code}
                  onChange={(e) => setSkillToSave({ ...skillToSave, python_code: e.target.value })}
                  rows={8}
                  className="w-full px-3 py-2 rounded-xl bg-slate-50 dark:bg-slate-950 text-slate-800 dark:text-slate-200 font-mono text-[11px] border border-slate-200 dark:border-slate-800 leading-relaxed focus:outline-hidden"
                />
              </div>
            </div>

            <div className="flex justify-end gap-2 pt-2">
              <Button variant="outline" size="sm" onClick={() => setIsSaveSkillModalOpen(false)}>
                暂不沉淀
              </Button>
              <Button size="sm" onClick={handleSaveSkillConfirm} className="bg-amber-600 hover:bg-amber-500 text-white font-semibold">
                确认沉淀为技能 ⚡
              </Button>
            </div>
          </div>
        </div>
      )}

      {/* 图片点击放大全屏灯箱预览 (Image Lightbox) */}
      {previewImage && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 backdrop-blur-xs p-4 sm:p-8 animate-in fade-in duration-150 select-none"
          onClick={() => setPreviewImage(null)}
        >
          <div
            className="relative max-w-5xl max-h-[90vh] flex flex-col items-center bg-slate-900/95 rounded-2xl border border-white/15 shadow-2xl overflow-hidden p-3.5 select-text"
            onClick={(e) => e.stopPropagation()}
          >
            {/* 顶部工具栏 */}
            <div className="w-full flex items-center justify-between pb-2 mb-2 border-b border-white/10 text-white/90 text-xs px-2 shrink-0">
              <span className="truncate max-w-md font-medium text-slate-200" title={previewImage.name || '图片预览'}>
                {previewImage.name || '图片预览'}
              </span>
              <div className="flex items-center gap-2">
                <a
                  href={previewImage.url}
                  target="_blank"
                  rel="noreferrer"
                  download={previewImage.name || 'image.png'}
                  className="p-1.5 rounded-lg bg-white/10 hover:bg-white/20 text-white transition-colors cursor-pointer"
                  title="在新标签页打开 / 下载原图"
                >
                  <ExternalLink className="h-4 w-4" />
                </a>
                <button
                  type="button"
                  onClick={() => setPreviewImage(null)}
                  className="p-1.5 rounded-lg bg-white/10 hover:bg-white/20 text-white transition-colors cursor-pointer"
                  title="关闭 (Esc)"
                >
                  <X className="h-4 w-4" />
                </button>
              </div>
            </div>

            {/* 原图展示区 */}
            <div className="flex-1 overflow-auto flex items-center justify-center max-h-[80vh] w-full p-1">
              <AuthenticatedImage
                src={previewImage.url}
                alt={previewImage.name || '预览'}
                className="max-w-full max-h-[75vh] object-contain rounded-lg shadow-md select-none"
              />
            </div>
          </div>
        </div>
      )}

      {/* 多智能体协同网络拓扑全息视图弹窗 */}
      <AgentTopologyViewer
        mode="modal"
        isOpen={showTopologyModal}
        onClose={() => setShowTopologyModal(false)}
        onSelectBot={(botId) => {
          setActiveBotId(botId)
          setShowTopologyModal(false)
        }}
        activeBotId={activeBotId}
      />
    </div>
  )
}
