import React, { useState } from 'react'
import {
  Lock,
  Sparkles,
  Copy,
  Check,
  Edit3,
  Settings,
  FileText,
  Layers,
  Code2,
  ChevronDown,
  ChevronRight,
  Info,
  Trash2,
  Zap,
  Cpu,
  Database,
  Globe,
  History,
  Box,
  Terminal,
} from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { MarkdownView } from '@/components/common/MarkdownView'
import { useApiStore } from '@/stores/apiStore'
import { LLMConfigData } from './types'

interface AgentPromptTabProps {
  activeBot: any
  llmConfig: LLMConfigData
  botFilesCount: number
  onSaveBasePrompt: (newPrompt: string) => Promise<void>
  isSavingBasePrompt: boolean
  onOpenEditProfileModal: () => void
  onOpenModelModal: () => void
  onDeleteActiveBot: () => void
  onDeleteGuideline: (id: string) => void
  onDeleteMemory: (key: string) => void
  onInspectSkill: (skill: any) => void
  showToast: (toast: { type: 'success' | 'error' | 'info'; message: string }, duration?: number) => void
}

export const AgentPromptTab: React.FC<AgentPromptTabProps> = ({
  activeBot,
  llmConfig,
  botFilesCount,
  onSaveBasePrompt,
  isSavingBasePrompt,
  onOpenEditProfileModal,
  onOpenModelModal,
  onDeleteActiveBot,
  onDeleteGuideline,
  onDeleteMemory,
  onInspectSkill,
  showToast,
}) => {
  const [promptViewMode, setPromptViewMode] = useState<'assembled' | 'base'>('assembled')
  const [blueprintViewMode, setBlueprintViewMode] = useState<'cards' | 'raw'>('cards')
  const [rawSubMode, setRawSubMode] = useState<'task' | 'system'>('task')
  const apis = useApiStore((state) => state.apis)
  const totalApisCount = apis.length || 427
  const [expandedBlueprintCards, setExpandedBlueprintCards] = useState<Record<string, boolean>>({
    mod1: true,
    mod2: false,
    mod3: true,
    mod4: true,
    mod5: true,
    mem_l3: true,
    mem_l2: true,
    mem_l4: true,
    mem_l1: true,
    mod6: false,
  })
  const [isEditingBasePrompt, setIsEditingBasePrompt] = useState<boolean>(false)
  const [editedBasePrompt, setEditedBasePrompt] = useState<string>('')
  const [copiedPromptId, setCopiedPromptId] = useState<string | null>(null)
  const [hasCopiedBotId, setHasCopiedBotId] = useState<boolean>(false)
  const canEditDefinition = Boolean(activeBot?.permissions?.can_edit_definition)

  const isAllBlueprintExpanded = Object.values(expandedBlueprintCards).every(Boolean)

  const toggleBlueprintCard = (modKey: string) => {
    setExpandedBlueprintCards((prev) => ({
      ...prev,
      [modKey]: !prev[modKey],
    }))
  }

  const toggleAllBlueprintCards = () => {
    const nextVal = !isAllBlueprintExpanded
    const nextState: Record<string, boolean> = {}
    Object.keys(expandedBlueprintCards).forEach((k) => {
      nextState[k] = nextVal
    })
    setExpandedBlueprintCards(nextState)
  }

  const handleCopyBotId = () => {
    if (activeBot?.id) {
      navigator.clipboard.writeText(activeBot.id)
      setHasCopiedBotId(true)
      setTimeout(() => setHasCopiedBotId(false), 2000)
      showToast({ type: 'success', message: '已复制智能体 ID' })
    }
  }

  return (
    <div className="space-y-3.5">
      {/* 智能体核心档案与指标看板 (参考 Dify / Coze 经典信息架构) */}
      <div className="p-3.5 rounded-2xl border border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900 shadow-2xs space-y-3">
        <div className="flex items-start justify-between gap-2">
          <div className="flex items-center gap-3">
            <div className="w-11 h-11 rounded-2xl bg-gradient-to-br from-blue-50 to-indigo-50 dark:from-blue-950/60 dark:to-indigo-950/40 border border-blue-200 dark:border-blue-800/80 flex items-center justify-center text-2xl shrink-0 shadow-2xs">
              {activeBot?.avatar || '🤖'}
            </div>
            <div>
              <div className="font-bold text-slate-900 dark:text-slate-100 text-xs flex items-center gap-2">
                <span>{activeBot?.name || '默认助手'}</span>
                {activeBot?.id === 'default_assistant' ? (
                  <Badge variant="outline" className="text-[10px] py-0 px-1.5 border-amber-300 dark:border-amber-700 bg-amber-50/70 dark:bg-amber-950/40 text-amber-700 dark:text-amber-300 font-semibold flex items-center gap-1 shrink-0">
                    <Lock className="h-2.5 w-2.5" />
                    <span>系统内置 · 只读保护</span>
                  </Badge>
                ) : (
                  <Badge variant="outline" className="text-[10px] py-0 px-1.5 border-blue-300 dark:border-blue-700 bg-blue-50/70 dark:bg-blue-950/40 text-blue-700 dark:text-blue-300 font-semibold flex items-center gap-1 shrink-0">
                    <Sparkles className="h-2.5 w-2.5 text-blue-500" />
                    <span>自定义智能体</span>
                  </Badge>
                )}
              </div>
              <div className="flex items-center gap-1.5 text-[10px] text-slate-400 font-mono mt-0.5">
                <span>ID: {activeBot?.id}</span>
                <button
                  type="button"
                  onClick={handleCopyBotId}
                  className="hover:text-blue-500 p-0.5 rounded cursor-pointer transition-colors"
                  title="复制唯一标识 (ID)"
                >
                  {hasCopiedBotId ? <Check className="h-2.5 w-2.5 text-emerald-500" /> : <Copy className="h-2.5 w-2.5" />}
                </button>
              </div>
            </div>
          </div>

          <div className="flex items-center gap-1.5">
            {activeBot?.id !== 'default_assistant' && canEditDefinition && (
              <button
                type="button"
                onClick={onOpenEditProfileModal}
                className="px-2 py-1 rounded-lg border border-slate-200 dark:border-slate-800 hover:bg-slate-50 dark:hover:bg-slate-800 text-[11px] text-slate-700 dark:text-slate-300 flex items-center gap-1 cursor-pointer transition-colors"
                title="修改智能体头像、名称与描述"
              >
                <Edit3 className="h-3 w-3 text-blue-500" />
                <span>编辑资料</span>
              </button>
            )}
            <button
              type="button"
              disabled={!canEditDefinition}
              onClick={onOpenModelModal}
              className="px-2 py-1 rounded-lg border border-slate-200 dark:border-slate-800 hover:bg-slate-50 dark:hover:bg-slate-800 text-[11px] text-slate-700 dark:text-slate-300 flex items-center gap-1 cursor-pointer transition-colors"
              title="配置专属推理模型"
            >
              <Settings className="h-3 w-3 text-slate-400" />
              <span>模型配置</span>
            </button>
          </div>
        </div>

        {activeBot?.description ? (
          <p className="text-[11px] text-slate-600 dark:text-slate-400 leading-relaxed bg-slate-50 dark:bg-slate-950/50 p-2.5 rounded-xl border border-slate-100 dark:border-slate-800/80">
            {activeBot.description}
          </p>
        ) : (
          activeBot?.id !== 'default_assistant' && canEditDefinition && (
            <div
              onClick={onOpenEditProfileModal}
              className="text-[11px] text-slate-400 dark:text-slate-500 border border-dashed border-slate-200 dark:border-slate-800 p-2 rounded-xl text-center cursor-pointer hover:border-blue-400 hover:text-blue-500 transition-colors"
            >
              + 点击补充业务描述
            </div>
          )
        )}

        {/* 运行时参数汇总与审计统计网格 */}
        <div className="grid grid-cols-2 gap-1.5 text-[10px] pt-1 border-t border-slate-100 dark:border-slate-800">
          <div className="flex items-center justify-between px-2.5 py-1.5 rounded-lg bg-slate-50 dark:bg-slate-950/70 border border-slate-100 dark:border-slate-800/60">
            <span className="text-slate-400">底层模型</span>
            <span className="font-semibold text-slate-700 dark:text-slate-300 truncate max-w-[110px]" title={activeBot?.model_override?.model_name || llmConfig.model_name}>
              {activeBot?.model_override?.model_name || llmConfig.model_name || 'deepseek-chat'}
            </span>
          </div>
          <div className="flex items-center justify-between px-2.5 py-1.5 rounded-lg bg-slate-50 dark:bg-slate-950/70 border border-slate-100 dark:border-slate-800/60">
            <span className="text-slate-400">历史互动</span>
            <span className="font-semibold text-slate-700 dark:text-slate-300 font-mono">
              {activeBot?.history_count || 0} 轮会话
            </span>
          </div>
          <div className="flex items-center justify-between px-2.5 py-1.5 rounded-lg bg-slate-50 dark:bg-slate-950/70 border border-slate-100 dark:border-slate-800/60">
            <span className="text-slate-400">累计消耗</span>
            <span className="font-semibold text-purple-600 dark:text-purple-400 font-mono" title="全生命周期累计 Token">
              {activeBot?.stats?.total_tokens ? `${activeBot.stats.total_tokens.toLocaleString()} tokens` : '0 tokens'}
            </span>
          </div>
          <div className="flex items-center justify-between px-2.5 py-1.5 rounded-lg bg-slate-50 dark:bg-slate-950/70 border border-slate-100 dark:border-slate-800/60">
            <span className="text-slate-400">沙箱执行</span>
            <span className="font-semibold text-emerald-600 dark:text-emerald-400 font-mono">
              {activeBot?.stats?.total_code_executions || 0} 次
            </span>
          </div>
          <div className="flex items-center justify-between px-2.5 py-1.5 rounded-lg bg-slate-50 dark:bg-slate-950/70 border border-slate-100 dark:border-slate-800/60">
            <span className="text-slate-400">产出物文件</span>
            <span className="font-semibold text-blue-600 dark:text-blue-400 font-mono">
              {activeBot?.files_count || botFilesCount || 0} 个
            </span>
          </div>
          <div className="flex items-center justify-between px-2.5 py-1.5 rounded-lg bg-slate-50 dark:bg-slate-950/70 border border-slate-100 dark:border-slate-800/60">
            <span className="text-slate-400">自进化沉淀</span>
            <span className="font-semibold text-amber-600 dark:text-amber-400 font-mono">
              {activeBot?.learned_guidelines?.length || 0} 准则 / {activeBot?.skills?.length || 0} 技能
            </span>
          </div>
        </div>
      </div>

      {/* 提示词查看与编辑控制台 (Dify 风格的分段布局) */}
      <div className="space-y-2.5">
        {/* 分段控制器 */}
        <div className="flex items-center p-1 rounded-xl bg-slate-100 dark:bg-slate-800/80 text-xs font-medium gap-1">
          <button
            type="button"
            onClick={() => setPromptViewMode('assembled')}
            className={`flex-1 py-1.5 rounded-lg transition-all cursor-pointer flex items-center justify-center gap-1.5 ${
              promptViewMode === 'assembled'
                ? 'bg-white dark:bg-slate-900 text-blue-600 dark:text-blue-400 shadow-2xs font-bold'
                : 'text-slate-500 hover:text-slate-700 dark:hover:text-slate-300'
            }`}
          >
            <Sparkles className="h-3.5 w-3.5 text-amber-500" />
            <span>实时装配全量透视</span>
          </button>
          <button
            type="button"
            onClick={() => setPromptViewMode('base')}
            className={`flex-1 py-1.5 rounded-lg transition-all cursor-pointer flex items-center justify-center gap-1.5 ${
              promptViewMode === 'base'
                ? 'bg-white dark:bg-slate-900 text-blue-600 dark:text-blue-400 shadow-2xs font-bold'
                : 'text-slate-500 hover:text-slate-700 dark:hover:text-slate-300'
            }`}
          >
            <FileText className="h-3.5 w-3.5 text-blue-500" />
            <span>基础设定 (Base)</span>
          </button>
        </div>

        {/* 视图 1: 实时全量装配 Prompt (含 模块透视 vs 完整原文 切换) */}
        {promptViewMode === 'assembled' && (
          <div className="space-y-3">
            <div className="flex items-center justify-between px-0.5">
              {/* 子切换: 模块卡片 vs Markdown 原文 */}
              <div className="flex items-center p-0.5 rounded-lg bg-slate-100 dark:bg-slate-800 text-[11px] font-medium">
                <button
                  type="button"
                  onClick={() => setBlueprintViewMode('cards')}
                  className={`px-2.5 py-1 rounded-md transition-all cursor-pointer flex items-center gap-1 ${
                    blueprintViewMode === 'cards'
                      ? 'bg-white dark:bg-slate-900 text-blue-600 dark:text-blue-400 shadow-2xs font-semibold'
                      : 'text-slate-500 hover:text-slate-700'
                  }`}
                >
                  <Layers className="h-3.5 w-3.5 text-blue-500" />
                  <span>模块透视</span>
                </button>
                <button
                  type="button"
                  onClick={() => setBlueprintViewMode('raw')}
                  className={`px-2.5 py-1 rounded-md transition-all cursor-pointer flex items-center gap-1 ${
                    blueprintViewMode === 'raw'
                      ? 'bg-white dark:bg-slate-900 text-blue-600 dark:text-blue-400 shadow-2xs font-semibold'
                      : 'text-slate-500 hover:text-slate-700'
                  }`}
                >
                  <Code2 className="h-3.5 w-3.5 text-indigo-500" />
                  <span>完整原文</span>
                </button>
              </div>

              <div className="flex items-center gap-2">
                {blueprintViewMode === 'cards' && (
                  <button
                    type="button"
                    onClick={toggleAllBlueprintCards}
                    className="text-xs px-2.5 py-1 rounded-lg text-slate-600 dark:text-slate-400 hover:text-slate-800 dark:hover:text-slate-200 hover:bg-slate-100 dark:hover:bg-slate-800 transition-colors cursor-pointer flex items-center gap-1 font-medium"
                    title={isAllBlueprintExpanded ? '收起所有模块' : '展开所有模块'}
                  >
                    {isAllBlueprintExpanded ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}
                    <span>{isAllBlueprintExpanded ? '全部收起' : '全部展开'}</span>
                  </button>
                )}
              </div>
            </div>

            {/* 四层分级记忆架构实时运行状态看板 */}
            {activeBot?.hierarchical_memory && (
              <div className="p-3.5 rounded-2xl border border-slate-200 dark:border-slate-800 bg-gradient-to-br from-slate-50 via-white to-slate-50 dark:from-slate-900 dark:via-slate-950 dark:to-slate-900 shadow-2xs space-y-2.5">
                <div className="flex items-center justify-between text-xs">
                  <div className="font-bold flex items-center gap-1.5 text-slate-800 dark:text-slate-100">
                    <Layers className="h-4 w-4 text-indigo-500" />
                    <span>四层分级记忆架构运行状态</span>
                  </div>
                  <span className="text-[10px] text-slate-400 font-mono">
                    会话总历史: {activeBot.hierarchical_memory.total_history_count} 轮
                  </span>
                </div>

                <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
                  {/* Layer 1 */}
                  <div className="p-2.5 rounded-xl bg-white dark:bg-slate-900 border border-emerald-200/80 dark:border-emerald-900/60 shadow-2xs">
                    <div className="text-[10px] text-emerald-600 dark:text-emerald-400 font-medium flex items-center gap-1">
                      <span className="w-1.5 h-1.5 rounded-full bg-emerald-500 animate-pulse"></span>
                      <span>L1 即时工作记忆</span>
                    </div>
                    <div className="text-xs font-bold text-slate-800 dark:text-slate-200 mt-1 font-mono">
                      {activeBot.hierarchical_memory.working_memory_turns} 轮高保真
                    </div>
                    <div className="text-[10px] text-slate-400 mt-0.5">
                      {Math.round((activeBot.hierarchical_memory.working_memory_chars || 0) / 1000)}k / {Math.round(activeBot.hierarchical_memory.context_budget / 1000)}k 字符 ({Math.min(100, Math.round(((activeBot.hierarchical_memory.working_memory_chars || 0) / activeBot.hierarchical_memory.context_budget) * 100))}%)
                    </div>
                  </div>

                  {/* Layer 2 */}
                  <div className="p-2.5 rounded-xl bg-white dark:bg-slate-900 border border-purple-200/80 dark:border-purple-900/60 shadow-2xs">
                    <div className="text-[10px] text-purple-600 dark:text-purple-400 font-medium flex items-center gap-1">
                      <span className="w-1.5 h-1.5 rounded-full bg-purple-500"></span>
                      <span>L2 演化滚动摘要</span>
                    </div>
                    <div className="text-xs font-bold text-slate-800 dark:text-slate-200 mt-1 font-mono">
                      {activeBot.hierarchical_memory.compacted_history_count > 0 ? `已折叠 ${activeBot.hierarchical_memory.compacted_history_count} 轮` : '暂未触发折叠'}
                    </div>
                    <div className="text-[10px] text-slate-400 mt-0.5">
                      {activeBot.hierarchical_memory.running_summary ? '业务主线常驻生效中' : '全量在即时工作区'}
                    </div>
                  </div>

                  {/* Layer 3 */}
                  <div className="p-2.5 rounded-xl bg-white dark:bg-slate-900 border border-blue-200/80 dark:border-blue-900/60 shadow-2xs">
                    <div className="text-[10px] text-blue-600 dark:text-blue-400 font-medium flex items-center gap-1">
                      <span className="w-1.5 h-1.5 rounded-full bg-blue-500"></span>
                      <span>L3 实体知识字典</span>
                    </div>
                    <div className="text-xs font-bold text-slate-800 dark:text-slate-200 mt-1 font-mono">
                      {activeBot.hierarchical_memory.entities_catalog?.groups?.length || 0} 个群资产
                    </div>
                    <div className="text-[10px] text-slate-400 mt-0.5">
                      {activeBot.hierarchical_memory.entities_catalog?.apis?.length || 0} 个核心接口跟踪
                    </div>
                  </div>

                  {/* Layer 4 */}
                  <div className="p-2.5 rounded-xl bg-white dark:bg-slate-900 border border-amber-200/80 dark:border-amber-900/60 shadow-2xs">
                    <div className="text-[10px] text-amber-600 dark:text-amber-400 font-medium flex items-center gap-1">
                      <span className="w-1.5 h-1.5 rounded-full bg-amber-500"></span>
                      <span>L4 沙箱运行时内存</span>
                    </div>
                    <div className="text-xs font-bold text-slate-800 dark:text-slate-200 mt-1 font-mono">
                      {Object.keys(activeBot.hierarchical_memory.runtime_variables || {}).length} 个活跃变量
                    </div>
                    <div className="text-[10px] text-slate-400 mt-0.5">
                      {Object.keys(activeBot.hierarchical_memory.runtime_variables || {}).length > 0 ? '内存数据免请求复用' : '沙箱初始纯净状态'}
                    </div>
                  </div>
                </div>
              </div>
            )}

            <div className="p-3 rounded-xl bg-blue-50/70 dark:bg-blue-950/40 border border-blue-200/80 dark:border-blue-800/60 text-xs text-blue-900 dark:text-blue-200 leading-relaxed flex items-start gap-2.5 shadow-2xs">
              <Info className="h-4 w-4 text-blue-500 shrink-0 mt-0.5" />
              <div>
                <strong>运行期全量装配透视</strong>：每次交互前，系统将以下人设基底、平台规范与四层分级记忆动态装配为送入大模型的完整上下文。点击各卡片头部可自由展开或收起全部内容。
              </div>
            </div>

            {/* 模块透视视图 (自然垂直滚动，彻底消除人工字符截断与双滚动条) */}
            {blueprintViewMode === 'cards' ? (
              <div className="space-y-3">
                {/* 模块 1: 角色定位与业务基底 */}
                <div className="rounded-2xl border border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900/90 shadow-2xs overflow-hidden transition-all">
                  <div
                    onClick={() => toggleBlueprintCard('mod1')}
                    className="p-3.5 flex items-center justify-between cursor-pointer hover:bg-slate-50/80 dark:hover:bg-slate-800/40 transition-colors select-none"
                  >
                    <div className="flex items-center gap-2.5 min-w-0">
                      <div className="w-7 h-7 rounded-lg bg-blue-50 dark:bg-blue-950/80 border border-blue-200 dark:border-blue-800 flex items-center justify-center text-sm shrink-0">
                        🎭
                      </div>
                      <div className="font-bold text-slate-800 dark:text-slate-100 text-xs flex items-center gap-2">
                        <span>模块 1：角色设定与定位基底</span>
                        <Badge variant="outline" className="text-[10px] py-0 px-1.5 border-blue-300 dark:border-blue-700 text-blue-600 font-normal">人设基底</Badge>
                      </div>
                    </div>

                    <div className="flex items-center gap-2 shrink-0">
                      <span className="text-xs text-slate-400 font-mono">
                        {activeBot?.base_prompt?.length || 0} 字
                      </span>
                      {expandedBlueprintCards.mod1 ? (
                        <ChevronDown className="h-4 w-4 text-slate-400" />
                      ) : (
                        <ChevronRight className="h-4 w-4 text-slate-400" />
                      )}
                    </div>
                  </div>

                  {expandedBlueprintCards.mod1 && (
                    <div className="p-3.5 pt-0 border-t border-slate-100 dark:border-slate-800/60 mt-1">
                      <div className="rounded-xl border border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-950 overflow-hidden shadow-2xs mt-2.5">
                        <div className="flex items-center justify-between px-3 py-1.5 bg-slate-100/80 dark:bg-slate-900 border-b border-slate-200 dark:border-slate-800 text-[11px] font-mono select-none">
                          <div className="flex items-center gap-1.5 text-slate-600 dark:text-slate-400">
                            <FileText className="h-3.5 w-3.5 text-blue-500" />
                            <span className="font-semibold text-slate-700 dark:text-slate-300">base_persona.md</span>
                            <span className="text-[10px] text-slate-400 font-normal">({activeBot?.base_prompt?.length || 0} 字)</span>
                          </div>
                          <button
                            type="button"
                            onClick={() => {
                              if (activeBot?.base_prompt) {
                                navigator.clipboard.writeText(activeBot.base_prompt)
                                setCopiedPromptId('mod1')
                                setTimeout(() => setCopiedPromptId(null), 2000)
                                showToast({ type: 'success', message: '已复制人设基底' })
                              }
                            }}
                            className="flex items-center gap-1 px-2 py-0.5 rounded-md hover:bg-slate-200 dark:hover:bg-slate-800 text-slate-500 hover:text-slate-800 dark:text-slate-400 dark:hover:text-slate-200 transition-colors cursor-pointer text-[10px]"
                            title="复制代码/文本"
                          >
                            {copiedPromptId === 'mod1' ? (
                              <>
                                <Check className="h-3 w-3 text-emerald-500" />
                                <span className="text-emerald-600 dark:text-emerald-400 font-semibold">已复制</span>
                              </>
                            ) : (
                              <>
                                <Copy className="h-3 w-3 text-slate-400" />
                                <span>复制</span>
                              </>
                            )}
                          </button>
                        </div>
                        <div className="p-3.5 max-h-80 overflow-y-auto font-mono text-xs leading-relaxed text-slate-800 dark:text-slate-200">
                          <MarkdownView
                            content={activeBot?.base_prompt || '暂无基础人设设定'}
                            className="text-slate-800 dark:text-slate-200 text-xs leading-relaxed"
                          />
                        </div>
                      </div>
                    </div>
                  )}
                </div>

                {/* 模块 2: 平台底座契约与全量云 API 调度规范 */}
                <div className="rounded-2xl border border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900/90 shadow-2xs overflow-hidden transition-all">
                  <div
                    onClick={() => toggleBlueprintCard('mod2')}
                    className="p-3.5 flex items-center justify-between cursor-pointer hover:bg-slate-50/80 dark:hover:bg-slate-800/40 transition-colors select-none"
                  >
                    <div className="flex items-center gap-2.5 min-w-0">
                      <div className="w-7 h-7 rounded-lg bg-emerald-50 dark:bg-emerald-950/80 border border-emerald-200 dark:border-emerald-800 flex items-center justify-center text-sm shrink-0">
                        📋
                      </div>
                      <div className="font-bold text-slate-800 dark:text-slate-100 text-xs flex items-center gap-2">
                        <span>模块 2：腾讯云多业务云架构与 API 调度原则</span>
                        <Badge variant="outline" className="text-[10px] py-0 px-1.5 border-emerald-300 dark:border-emerald-700 text-emerald-600 font-normal">底座契约</Badge>
                      </div>
                    </div>

                    <div className="flex items-center gap-2 shrink-0">
                      <span className="text-xs text-slate-400">{totalApisCount}+ Cloud APIs</span>
                      {expandedBlueprintCards.mod2 ? (
                        <ChevronDown className="h-4 w-4 text-slate-400" />
                      ) : (
                        <ChevronRight className="h-4 w-4 text-slate-400" />
                      )}
                    </div>
                  </div>

                  {expandedBlueprintCards.mod2 && (
                    <div className="p-3.5 pt-0 space-y-2.5 border-t border-slate-100 dark:border-slate-800/60 mt-1">
                      <div className="p-3 rounded-xl bg-slate-50/70 dark:bg-slate-950/70 border border-slate-200/80 dark:border-slate-800 text-xs text-slate-700 dark:text-slate-300 leading-relaxed space-y-2 mt-2">
                        <div className="flex items-start gap-2">
                          <span className="text-emerald-500 font-bold shrink-0">1.</span>
                          <div>
                            <strong>全量接口调度与检索权限</strong>：拥有直接调用腾讯云多业务全量接口（涵盖云直播 API 3.0、即时通信 IM REST 及任意已注册云服务）的最高权限（<code>cloud_call</code>、<code>live_call</code>、<code>im_call</code>、<code>tim_call</code>），以及接口语义检索（<code>cloud_search_apis</code>）和官方文档查阅（<code>cloud_get_doc</code>）能力。
                          </div>
                        </div>
                        <div className="flex items-start gap-2">
                          <span className="text-emerald-500 font-bold shrink-0">2.</span>
                          <div>
                            <strong>Python 沙箱批量自动化</strong>：当需要执行批量操作（如批量禁言、循环撤回、拉取多页群成员并清洗过滤）时，直接编写并执行 Python 脚本，以 for 循环和条件判断高效闭环完成。
                          </div>
                        </div>
                        <div className="flex items-start gap-2">
                          <span className="text-emerald-500 font-bold shrink-0">3.</span>
                          <div>
                            <strong>严谨客观与安全审计</strong>：保持回答专业、精炼、客观，并在调用关键接口或修改系统配置后，明确汇总操作结果和受影响的账号或群组列表。
                          </div>
                        </div>
                      </div>
                    </div>
                  )}
                </div>

                {/* 模块 3: 自我强化的专属行为准则 */}
                <div className="rounded-2xl border border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900/90 shadow-2xs overflow-hidden transition-all">
                  <div
                    onClick={() => toggleBlueprintCard('mod3')}
                    className="p-3.5 flex items-center justify-between cursor-pointer hover:bg-slate-50/80 dark:hover:bg-slate-800/40 transition-colors select-none"
                  >
                    <div className="flex items-center gap-2.5 min-w-0">
                      <div className="w-7 h-7 rounded-lg bg-blue-50 dark:bg-blue-950/80 border border-blue-200 dark:border-blue-800 flex items-center justify-center text-sm shrink-0">
                        🛡️
                      </div>
                      <div className="font-bold text-slate-800 dark:text-slate-100 text-xs flex items-center gap-2">
                        <span>模块 3：自我强化的专属行为准则</span>
                        <Badge variant="outline" className="text-[10px] py-0 px-1.5 border-blue-300 dark:border-blue-700 text-blue-600 font-normal">无感进化</Badge>
                      </div>
                    </div>

                    <div className="flex items-center gap-2 shrink-0">
                      <span className="text-xs font-semibold text-blue-600 dark:text-blue-400 font-mono">
                        {activeBot?.learned_guidelines?.length || 0} 条
                      </span>
                      {expandedBlueprintCards.mod3 ? (
                        <ChevronDown className="h-4 w-4 text-slate-400" />
                      ) : (
                        <ChevronRight className="h-4 w-4 text-slate-400" />
                      )}
                    </div>
                  </div>

                  {expandedBlueprintCards.mod3 && (
                    <div className="p-3.5 pt-0 space-y-2 border-t border-slate-100 dark:border-slate-800/60 mt-1">
                      {activeBot?.learned_guidelines?.length > 0 ? (
                        <div className="space-y-2 pt-2">
                          {activeBot.learned_guidelines.map((g: any, idx: number) => (
                            <div
                              key={g.id || idx}
                              className="p-3 rounded-xl bg-slate-50/70 dark:bg-slate-950/70 border border-slate-200/80 dark:border-slate-800 space-y-1.5"
                            >
                              <div className="flex items-start justify-between gap-2">
                                <div className="text-xs font-medium text-slate-800 dark:text-slate-200 leading-relaxed break-words">
                                  • {g.instruction}
                                </div>
                                <button
                                  type="button"
                                  disabled={!canEditDefinition}
                                  onClick={(e) => {
                                    e.stopPropagation()
                                    onDeleteGuideline(g.id)
                                  }}
                                  className="text-slate-400 hover:text-red-500 p-1 rounded-md cursor-pointer transition-colors shrink-0"
                                  title="删除此条行为准则"
                                >
                                  <Trash2 className="h-3 w-3" />
                                </button>
                              </div>
                              {g.reason && (
                                <div className="text-[11px] text-blue-600 dark:text-blue-400">
                                  背景说明：{g.reason}
                                </div>
                              )}
                            </div>
                          ))}
                        </div>
                      ) : (
                        <div className="p-3 rounded-xl bg-slate-50/60 dark:bg-slate-950/60 border border-dashed border-slate-200 dark:border-slate-800 text-xs text-slate-400 text-center italic mt-2">
                          {activeBot?.visibility === 'public' ? '尚无共享准则。个人行为偏好自动保存在当前用户的记忆中。' : '尚无沉淀准则，可在日常交流中表达长期要求。'}
                        </div>
                      )}
                    </div>
                  )}
                </div>

                {/* 模块 4: 长期事实记忆库 */}
                <div className="rounded-2xl border border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900/90 shadow-2xs overflow-hidden transition-all">
                  <div
                    onClick={() => toggleBlueprintCard('mod4')}
                    className="p-3.5 flex items-center justify-between cursor-pointer hover:bg-slate-50/80 dark:hover:bg-slate-800/40 transition-colors select-none"
                  >
                    <div className="flex items-center gap-2.5 min-w-0">
                      <div className="w-7 h-7 rounded-lg bg-purple-50 dark:bg-purple-950/80 border border-purple-200 dark:border-purple-800 flex items-center justify-center text-sm shrink-0">
                        🧠
                      </div>
                      <div className="font-bold text-slate-800 dark:text-slate-100 text-xs flex items-center gap-2">
                        <span>模块 4：长期事实记忆库</span>
                        <Badge variant="outline" className="text-[10px] py-0 px-1.5 border-purple-300 dark:border-purple-700 text-purple-600 font-normal">事实沉淀</Badge>
                      </div>
                    </div>

                    <div className="flex items-center gap-2 shrink-0">
                      <span className="text-xs font-semibold text-purple-600 dark:text-purple-400 font-mono">
                        {activeBot?.memories?.length || 0} 条
                      </span>
                      {expandedBlueprintCards.mod4 ? (
                        <ChevronDown className="h-4 w-4 text-slate-400" />
                      ) : (
                        <ChevronRight className="h-4 w-4 text-slate-400" />
                      )}
                    </div>
                  </div>

                  {expandedBlueprintCards.mod4 && (
                    <div className="p-3.5 pt-0 space-y-2 border-t border-slate-100 dark:border-slate-800/60 mt-1">
                      {activeBot?.memories?.length > 0 ? (
                        <div className="space-y-2 pt-2">
                          {activeBot.memories.map((m: any, idx: number) => (
                            <div
                              key={m.key || idx}
                              className="p-3 rounded-xl bg-slate-50/70 dark:bg-slate-950/70 border border-slate-200/80 dark:border-slate-800 flex items-start justify-between gap-3"
                            >
                              <div className="space-y-1 min-w-0 flex-1">
                                <div className="font-mono text-xs font-bold text-purple-600 dark:text-purple-400">
                                  {m.key}
                                </div>
                                <div className="text-xs text-slate-700 dark:text-slate-300 break-words leading-relaxed font-mono">
                                  {m.value || m.content}
                                </div>
                              </div>
                              <button
                                type="button"
                                onClick={(e) => {
                                  e.stopPropagation()
                                  onDeleteMemory(m.key)
                                }}
                                className="text-slate-400 hover:text-red-500 p-1 rounded-md cursor-pointer transition-colors shrink-0"
                                title="删除此条事实记忆"
                              >
                                <Trash2 className="h-3 w-3" />
                              </button>
                            </div>
                          ))}
                        </div>
                      ) : (
                        <div className="p-3 rounded-xl bg-slate-50/60 dark:bg-slate-950/60 border border-dashed border-slate-200 dark:border-slate-800 text-xs text-slate-400 text-center italic mt-2">
                          尚无记忆事实。对话中提及的关键群 ID、管理员账号等信息将自动沉淀在长期记忆中。
                        </div>
                      )}
                    </div>
                  )}
                </div>

                {/* 模块 5: 专属挂载技能库 */}
                <div className="rounded-2xl border border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900/90 shadow-2xs overflow-hidden transition-all">
                  <div
                    onClick={() => toggleBlueprintCard('mod5')}
                    className="p-3.5 flex items-center justify-between cursor-pointer hover:bg-slate-50/80 dark:hover:bg-slate-800/40 transition-colors select-none"
                  >
                    <div className="flex items-center gap-2.5 min-w-0">
                      <div className="w-7 h-7 rounded-lg bg-amber-50 dark:bg-amber-950/80 border border-amber-200 dark:border-amber-800 flex items-center justify-center text-sm shrink-0">
                        ⚡
                      </div>
                      <div className="font-bold text-slate-800 dark:text-slate-100 text-xs flex items-center gap-2">
                        <span>模块 5：已挂载专属技能库</span>
                        <Badge variant="outline" className="text-[10px] py-0 px-1.5 border-amber-300 dark:border-amber-700 text-amber-600 font-normal">技能固化</Badge>
                      </div>
                    </div>

                    <div className="flex items-center gap-2 shrink-0">
                      <span className="text-xs font-semibold text-amber-600 dark:text-amber-400 font-mono">
                        {activeBot?.skills?.length || 0} 个
                      </span>
                      {expandedBlueprintCards.mod5 ? (
                        <ChevronDown className="h-4 w-4 text-slate-400" />
                      ) : (
                        <ChevronRight className="h-4 w-4 text-slate-400" />
                      )}
                    </div>
                  </div>

                  {expandedBlueprintCards.mod5 && (
                    <div className="p-3.5 pt-0 space-y-2 border-t border-slate-100 dark:border-slate-800/60 mt-1">
                      {activeBot?.skills?.length > 0 ? (
                        <div className="space-y-2 pt-2">
                          {activeBot.skills.map((s: any, idx: number) => (
                            <div
                              key={s.name || idx}
                              className="p-3 rounded-xl bg-slate-50/70 dark:bg-slate-950/70 border border-slate-200/80 dark:border-slate-800 flex items-start justify-between gap-3"
                            >
                              <div className="space-y-1 min-w-0 flex-1">
                                <div className="font-mono text-xs font-bold text-amber-600 dark:text-amber-400 flex items-center gap-1.5">
                                  <Zap className="h-3 w-3" />
                                  <span>{s.name}</span>
                                </div>
                                <div className="text-xs text-slate-600 dark:text-slate-300 leading-normal">
                                  {s.description}
                                </div>
                              </div>
                              <button
                                type="button"
                                onClick={(e) => {
                                  e.stopPropagation()
                                  onInspectSkill(s)
                                }}
                                className="px-2.5 py-1 rounded-lg border border-slate-200 dark:border-slate-800 hover:bg-white dark:hover:bg-slate-800 text-[11px] text-slate-700 dark:text-slate-300 shrink-0 font-medium cursor-pointer transition-colors"
                                title="查看技能 Python 执行源码"
                              >
                                查看代码
                              </button>
                            </div>
                          ))}
                        </div>
                      ) : (
                        <div className="p-3 rounded-xl bg-slate-50/60 dark:bg-slate-950/60 border border-dashed border-slate-200 dark:border-slate-800 text-xs text-slate-400 text-center italic mt-2">
                          暂未装配技能。多步复杂业务流执行成功后，可由智能体自主沉淀为专属技能。
                        </div>
                      )}
                    </div>
                  )}
                </div>

                {/* 模块 6: 会话演化全局业务实体字典 (Layer 3: Entity Catalog) */}
                <div className="rounded-2xl border border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900/90 shadow-2xs overflow-hidden transition-all">
                  <div
                    onClick={() => toggleBlueprintCard('mem_l3')}
                    className="p-3.5 flex items-center justify-between cursor-pointer hover:bg-slate-50/80 dark:hover:bg-slate-800/40 transition-colors select-none"
                  >
                    <div className="flex items-center gap-2.5 min-w-0">
                      <div className="w-7 h-7 rounded-lg bg-blue-50 dark:bg-blue-950/80 border border-blue-200 dark:border-blue-800 flex items-center justify-center text-sm shrink-0">
                        🌐
                      </div>
                      <div className="font-bold text-slate-800 dark:text-slate-100 text-xs flex items-center gap-2">
                        <span>模块 6：会话演化全局业务实体字典</span>
                        <Badge variant="outline" className="text-[10px] py-0 px-1.5 border-blue-300 dark:border-blue-700 text-blue-600 font-normal">Layer 3 实体知识</Badge>
                      </div>
                    </div>

                    <div className="flex items-center gap-2 shrink-0">
                      <span className="text-xs text-slate-400 font-mono">
                        {(activeBot?.hierarchical_memory?.entities_catalog?.groups?.length || 0)} 群 / {(activeBot?.hierarchical_memory?.entities_catalog?.apis?.length || 0)} 接口
                      </span>
                      {expandedBlueprintCards.mem_l3 ? (
                        <ChevronDown className="h-4 w-4 text-slate-400" />
                      ) : (
                        <ChevronRight className="h-4 w-4 text-slate-400" />
                      )}
                    </div>
                  </div>

                  {expandedBlueprintCards.mem_l3 && (
                    <div className="p-3.5 pt-0 space-y-2.5 border-t border-slate-100 dark:border-slate-800/60 mt-1">
                      {activeBot?.hierarchical_memory?.entities_catalog?.groups?.length ? (
                        <div className="space-y-1.5 mt-2">
                          <div className="text-[11px] font-semibold text-slate-700 dark:text-slate-300 flex items-center gap-1">
                            <span>已知业务群组 (跨多轮自动追踪):</span>
                          </div>
                          <div className="flex flex-wrap gap-1.5 max-h-32 overflow-y-auto p-2 rounded-xl bg-slate-50 dark:bg-slate-950/70 border border-slate-200/70 dark:border-slate-800">
                            {activeBot.hierarchical_memory.entities_catalog.groups.map((gid: string) => (
                              <span key={gid} className="px-2 py-0.5 rounded-md bg-blue-50 dark:bg-blue-950 text-blue-700 dark:text-blue-300 border border-blue-200 dark:border-blue-800 text-[10px] font-mono">
                                {gid}
                              </span>
                            ))}
                          </div>
                        </div>
                      ) : (
                        <div className="p-3 rounded-xl bg-slate-50/60 dark:bg-slate-950/60 border border-dashed border-slate-200 dark:border-slate-800 text-xs text-slate-400 text-center italic mt-2">
                          暂无提取到的群组实体，对话中提及群号时系统将自动持久化至知识字典。
                        </div>
                      )}

                      {activeBot?.hierarchical_memory?.entities_catalog?.apis?.length ? (
                        <div className="space-y-1.5">
                          <div className="text-[11px] font-semibold text-slate-700 dark:text-slate-300">
                            核心调用接口追踪:
                          </div>
                          <div className="flex flex-wrap gap-1.5 p-2 rounded-xl bg-slate-50 dark:bg-slate-950/70 border border-slate-200/70 dark:border-slate-800">
                            {activeBot.hierarchical_memory.entities_catalog.apis.map((api: string) => (
                              <span key={api} className="px-2 py-0.5 rounded-md bg-indigo-50 dark:bg-indigo-950 text-indigo-700 dark:text-indigo-300 border border-indigo-200 dark:border-indigo-800 text-[10px] font-mono">
                                {api}
                              </span>
                            ))}
                          </div>
                        </div>
                      ) : null}
                    </div>
                  )}
                </div>

                {/* 模块 7: 会话演化滚动摘要 (Layer 2: Running Session Summary) */}
                <div className="rounded-2xl border border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900/90 shadow-2xs overflow-hidden transition-all">
                  <div
                    onClick={() => toggleBlueprintCard('mem_l2')}
                    className="p-3.5 flex items-center justify-between cursor-pointer hover:bg-slate-50/80 dark:hover:bg-slate-800/40 transition-colors select-none"
                  >
                    <div className="flex items-center gap-2.5 min-w-0">
                      <div className="w-7 h-7 rounded-lg bg-purple-50 dark:bg-purple-950/80 border border-purple-200 dark:border-purple-800 flex items-center justify-center text-sm shrink-0">
                        📜
                      </div>
                      <div className="font-bold text-slate-800 dark:text-slate-100 text-xs flex items-center gap-2">
                        <span>模块 7：会话演化滚动摘要</span>
                        <Badge variant="outline" className="text-[10px] py-0 px-1.5 border-purple-300 dark:border-purple-700 text-purple-600 font-normal">Layer 2 长期主线</Badge>
                      </div>
                    </div>

                    <div className="flex items-center gap-2 shrink-0">
                      <span className="text-xs text-slate-400 font-mono">
                        {activeBot?.hierarchical_memory?.compacted_history_count ? `已折叠 ${activeBot.hierarchical_memory.compacted_history_count} 轮` : '暂未折叠'}
                      </span>
                      {expandedBlueprintCards.mem_l2 ? (
                        <ChevronDown className="h-4 w-4 text-slate-400" />
                      ) : (
                        <ChevronRight className="h-4 w-4 text-slate-400" />
                      )}
                    </div>
                  </div>

                  {expandedBlueprintCards.mem_l2 && (
                    <div className="p-3.5 pt-0 space-y-2 border-t border-slate-100 dark:border-slate-800/60 mt-1">
                      {activeBot?.hierarchical_memory?.running_summary ? (
                        <div className="mt-2.5 rounded-xl border border-purple-200/70 dark:border-purple-900/50 bg-purple-50/30 dark:bg-purple-950/20 p-3 text-xs leading-relaxed font-mono">
                          <MarkdownView
                            content={activeBot.hierarchical_memory.running_summary}
                            className="text-xs text-slate-700 dark:text-slate-300"
                          />
                        </div>
                      ) : (
                        <div className="p-3 rounded-xl bg-slate-50/60 dark:bg-slate-950/60 border border-dashed border-slate-200 dark:border-slate-800 text-xs text-slate-400 text-center italic mt-2">
                          当前会话历史尚未超过工作记忆预算阈值（{activeBot?.hierarchical_memory?.max_context_turns || 25} 轮），所有对话均在 Layer 1 工作记忆中高保真保留。
                        </div>
                      )}
                    </div>
                  )}
                </div>

                {/* 模块 8: Python 沙箱常驻运行时变量 (Layer 4: Execution Runtime Memory) */}
                <div className="rounded-2xl border border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900/90 shadow-2xs overflow-hidden transition-all">
                  <div
                    onClick={() => toggleBlueprintCard('mem_l4')}
                    className="p-3.5 flex items-center justify-between cursor-pointer hover:bg-slate-50/80 dark:hover:bg-slate-800/40 transition-colors select-none"
                  >
                    <div className="flex items-center gap-2.5 min-w-0">
                      <div className="w-7 h-7 rounded-lg bg-amber-50 dark:bg-amber-950/80 border border-amber-200 dark:border-amber-800 flex items-center justify-center text-sm shrink-0">
                        ⚡
                      </div>
                      <div className="font-bold text-slate-800 dark:text-slate-100 text-xs flex items-center gap-2">
                        <span>模块 8：Python 沙箱常驻运行时变量</span>
                        <Badge variant="outline" className="text-[10px] py-0 px-1.5 border-amber-300 dark:border-amber-700 text-amber-600 font-normal">Layer 4 沙箱内存</Badge>
                      </div>
                    </div>

                    <div className="flex items-center gap-2 shrink-0">
                      <span className="text-xs text-slate-400 font-mono">
                        {Object.keys(activeBot?.hierarchical_memory?.runtime_variables || {}).length} 个变量
                      </span>
                      {expandedBlueprintCards.mem_l4 ? (
                        <ChevronDown className="h-4 w-4 text-slate-400" />
                      ) : (
                        <ChevronRight className="h-4 w-4 text-slate-400" />
                      )}
                    </div>
                  </div>

                  {expandedBlueprintCards.mem_l4 && (
                    <div className="p-3.5 pt-0 space-y-2 border-t border-slate-100 dark:border-slate-800/60 mt-1">
                      {activeBot?.hierarchical_memory?.runtime_variables && Object.keys(activeBot.hierarchical_memory.runtime_variables).length > 0 ? (
                        <div className="space-y-1.5 mt-2">
                          {Object.entries(activeBot.hierarchical_memory.runtime_variables).map(([name, desc]) => (
                            <div key={name} className="p-2.5 rounded-xl bg-slate-50 dark:bg-slate-950 border border-slate-200 dark:border-slate-800 flex items-center justify-between">
                              <div className="min-w-0">
                                <span className="font-mono font-bold text-amber-600 dark:text-amber-400 text-xs">`{name}`</span>
                                <span className="text-slate-600 dark:text-slate-300 text-xs ml-2 font-mono">{desc as string}</span>
                              </div>
                              <span className="text-[10px] text-emerald-600 dark:text-emerald-400 bg-emerald-50 dark:bg-emerald-950/70 border border-emerald-200 dark:border-emerald-800 px-1.5 py-0.5 rounded font-mono shrink-0">
                                内存驻留
                              </span>
                            </div>
                          ))}
                        </div>
                      ) : (
                        <div className="p-3 rounded-xl bg-slate-50/60 dark:bg-slate-950/60 border border-dashed border-slate-200 dark:border-slate-800 text-xs text-slate-400 text-center italic mt-2">
                          当前沙箱环境处于初始纯净态。智能体在执行数据计算或拉取后，变量将在此处常驻供后续脚本直接读取复用。
                        </div>
                      )}
                    </div>
                  )}
                </div>

                {/* 模块 9: 即时高保真工作区与观测蒸馏 (Layer 1: Working Memory) */}
                <div className="rounded-2xl border border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900/90 shadow-2xs overflow-hidden transition-all">
                  <div
                    onClick={() => toggleBlueprintCard('mem_l1')}
                    className="p-3.5 flex items-center justify-between cursor-pointer hover:bg-slate-50/80 dark:hover:bg-slate-800/40 transition-colors select-none"
                  >
                    <div className="flex items-center gap-2.5 min-w-0">
                      <div className="w-7 h-7 rounded-lg bg-emerald-50 dark:bg-emerald-950/80 border border-emerald-200 dark:border-emerald-800 flex items-center justify-center text-sm shrink-0">
                        💬
                      </div>
                      <div className="font-bold text-slate-800 dark:text-slate-100 text-xs flex items-center gap-2">
                        <span>模块 9：即时高保真工作区与观测蒸馏</span>
                        <Badge variant="outline" className="text-[10px] py-0 px-1.5 border-emerald-300 dark:border-emerald-700 text-emerald-600 font-normal">Layer 1 工作记忆</Badge>
                      </div>
                    </div>

                    <div className="flex items-center gap-2 shrink-0">
                      <span className="text-xs text-slate-400 font-mono">
                        {activeBot?.hierarchical_memory?.working_memory_turns || 0} 轮 / {Math.round((activeBot?.hierarchical_memory?.working_memory_chars || 0) / 1000)}k 字符
                      </span>
                      {expandedBlueprintCards.mem_l1 ? (
                        <ChevronDown className="h-4 w-4 text-slate-400" />
                      ) : (
                        <ChevronRight className="h-4 w-4 text-slate-400" />
                      )}
                    </div>
                  </div>

                  {expandedBlueprintCards.mem_l1 && (
                    <div className="p-3.5 pt-0 space-y-2.5 border-t border-slate-100 dark:border-slate-800/60 mt-1">
                      <div className="mt-2.5 p-3 rounded-xl bg-slate-50/70 dark:bg-slate-950/70 border border-slate-200/80 dark:border-slate-800 text-xs space-y-2">
                        <div className="flex items-center justify-between text-slate-600 dark:text-slate-400 text-[11px]">
                          <span>字符预算利用率:</span>
                          <span className="font-mono font-bold text-slate-800 dark:text-slate-200">
                            {activeBot?.hierarchical_memory?.working_memory_chars || 0} / {activeBot?.hierarchical_memory?.context_budget || 32000} 字符 ({Math.min(100, Math.round(((activeBot?.hierarchical_memory?.working_memory_chars || 0) / (activeBot?.hierarchical_memory?.context_budget || 32000)) * 100))}%)
                          </span>
                        </div>
                        <div className="w-full bg-slate-200 dark:bg-slate-800 h-2 rounded-full overflow-hidden">
                          <div
                            className="bg-emerald-500 h-full rounded-full transition-all"
                            style={{
                              width: `${Math.min(100, Math.round(((activeBot?.hierarchical_memory?.working_memory_chars || 0) / (activeBot?.hierarchical_memory?.context_budget || 32000)) * 100))}%`
                            }}
                          ></div>
                        </div>
                        <div className="text-[10px] text-slate-400 flex items-center justify-between pt-1">
                          <span>✨ 历史执行过程已启用智能观测蒸馏 (Observation Distillation)</span>
                          <span>上限 {activeBot?.hierarchical_memory?.max_context_turns || 25} 轮</span>
                        </div>
                      </div>
                    </div>
                  )}
                </div>

                {/* 模块 10: 自主进化契约 */}
                <div className="rounded-2xl border border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900/90 shadow-2xs overflow-hidden transition-all">
                  <div
                    onClick={() => toggleBlueprintCard('mod6')}
                    className="p-3.5 flex items-center justify-between cursor-pointer hover:bg-slate-50/80 dark:hover:bg-slate-800/40 transition-colors select-none"
                  >
                    <div className="flex items-center gap-2.5 min-w-0">
                      <div className="w-7 h-7 rounded-lg bg-indigo-50 dark:bg-indigo-950/80 border border-indigo-200 dark:border-indigo-800 flex items-center justify-center text-sm shrink-0">
                        💡
                      </div>
                      <div className="font-bold text-slate-800 dark:text-slate-100 text-xs flex items-center gap-2">
                        <span>模块 10：自主进化与无感沉淀规则</span>
                        <Badge variant="outline" className="text-[10px] py-0 px-1.5 border-indigo-300 dark:border-indigo-700 text-indigo-600 font-normal">核心准则</Badge>
                      </div>
                    </div>

                    <div className="flex items-center gap-2 shrink-0">
                      <span className="text-xs text-slate-400">零打扰原则</span>
                      {expandedBlueprintCards.mod6 ? (
                        <ChevronDown className="h-4 w-4 text-slate-400" />
                      ) : (
                        <ChevronRight className="h-4 w-4 text-slate-400" />
                      )}
                    </div>
                  </div>

                  {expandedBlueprintCards.mod6 && (
                    <div className="p-3.5 pt-0 space-y-2 border-t border-slate-100 dark:border-slate-800/60 mt-1">
                      <div className="p-3 rounded-xl bg-slate-50/70 dark:bg-slate-950/70 border border-slate-200/80 dark:border-slate-800 text-xs text-slate-700 dark:text-slate-300 leading-relaxed space-y-1.5 mt-2">
                        <div className="flex items-start gap-2">
                          <span className="text-indigo-500 font-bold shrink-0">1.</span>
                          <div>
                            <strong>对用户完全无感（核心铁律）</strong>：绝对禁止在给用户的回复中询问“是否沉淀为技能”、“是否记录为记忆”等确认语或弹窗。
                          </div>
                        </div>
                        <div className="flex items-start gap-2">
                          <span className="text-indigo-500 font-bold shrink-0">2.</span>
                          <div>
                            <strong>自主沉淀技能 (<code>save_skill</code>)</strong>：编写并执行成功具备高复用价值的复杂流时，自主在后台沉淀固化。
                          </div>
                        </div>
                        <div className="flex items-start gap-2">
                          <span className="text-indigo-500 font-bold shrink-0">3.</span>
                          <div>
                            <strong>自主强化准则 (<code>enhance_self_prompt</code>)</strong>：识别用户的长期习惯或约束要求时，自主固化为专属行为准则。
                          </div>
                        </div>
                        <div className="flex items-start gap-2">
                          <span className="text-indigo-500 font-bold shrink-0">4.</span>
                          <div>
                            <strong>自主事实记忆 (<code>save_memory</code>)</strong>：识别关键业务实体（群组、账号、标识）时，自主存入长期记忆库。
                          </div>
                        </div>
                        <div className="flex items-start gap-2">
                          <span className="text-indigo-500 font-bold shrink-0">5.</span>
                          <div>
                            <strong>回答精炼专注</strong>：最终回答清晰专注业务结果与排查报表，不赘述后台沉淀动作。
                          </div>
                        </div>
                      </div>
                    </div>
                  )}
                </div>
              </div>
            ) : (
              /* Markdown 完整原文视图 (含 运行期全量送模 vs 静态底座 双子视图) */
              <div className="rounded-2xl border border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-950 overflow-hidden shadow-2xs">
                <div className="flex flex-col sm:flex-row sm:items-center justify-between px-3.5 py-2.5 bg-slate-100/90 dark:bg-slate-900 border-b border-slate-200 dark:border-slate-800 text-xs font-mono select-none gap-2">
                  {/* 子选项卡: 运行期送模全量 vs 静态基础 System Prompt */}
                  <div className="flex items-center gap-1 bg-slate-200/80 dark:bg-slate-800 p-0.5 rounded-lg text-[11px]">
                    <button
                      type="button"
                      onClick={() => setRawSubMode('task')}
                      className={`px-2.5 py-1 rounded-md transition-all cursor-pointer flex items-center gap-1 ${
                        rawSubMode === 'task'
                          ? 'bg-white dark:bg-slate-900 text-blue-600 dark:text-blue-400 font-bold shadow-2xs'
                          : 'text-slate-500 hover:text-slate-700'
                      }`}
                    >
                      <Sparkles className="h-3 w-3 text-amber-500" />
                      <span>运行期送模全量 (含四层记忆)</span>
                    </button>
                    <button
                      type="button"
                      onClick={() => setRawSubMode('system')}
                      className={`px-2.5 py-1 rounded-md transition-all cursor-pointer flex items-center gap-1 ${
                        rawSubMode === 'system'
                          ? 'bg-white dark:bg-slate-900 text-blue-600 dark:text-blue-400 font-bold shadow-2xs'
                          : 'text-slate-500 hover:text-slate-700'
                      }`}
                    >
                      <FileText className="h-3 w-3 text-blue-500" />
                      <span>静态底座 (System Prompt)</span>
                    </button>
                  </div>

                  <div className="flex items-center gap-2 justify-end">
                    <span className="text-[11px] text-slate-400 font-normal">
                      ({(rawSubMode === 'task'
                        ? activeBot?.hierarchical_memory?.assembled_task_prompt?.length || activeBot?.assembled_prompt?.length || 0
                        : activeBot?.assembled_prompt?.length || 0)} 字)
                    </span>
                    <button
                      type="button"
                      onClick={() => {
                        const targetText =
                          rawSubMode === 'task'
                            ? activeBot?.hierarchical_memory?.assembled_task_prompt || activeBot?.assembled_prompt
                            : activeBot?.assembled_prompt
                        if (targetText) {
                          navigator.clipboard.writeText(targetText)
                          setCopiedPromptId('assembled-raw')
                          setTimeout(() => setCopiedPromptId(null), 2000)
                          showToast({ type: 'success', message: '已复制提示词原文' })
                        }
                      }}
                      className="flex items-center gap-1 px-2.5 py-1 rounded-md hover:bg-slate-200 dark:hover:bg-slate-800 text-slate-500 hover:text-slate-800 dark:text-slate-400 dark:hover:text-slate-200 transition-colors cursor-pointer text-xs shrink-0"
                      title="复制完整提示词"
                    >
                      {copiedPromptId === 'assembled-raw' ? (
                        <>
                          <Check className="h-3.5 w-3.5 text-emerald-500" />
                          <span className="text-emerald-600 font-semibold text-[11px]">已复制</span>
                        </>
                      ) : (
                        <>
                          <Copy className="h-3.5 w-3.5 text-slate-400" />
                          <span className="text-[11px]">复制</span>
                        </>
                      )}
                    </button>
                  </div>
                </div>
                <div className="p-4 max-h-[560px] overflow-y-auto font-mono text-xs leading-relaxed text-slate-800 dark:text-slate-200">
                  <MarkdownView
                    content={
                      (rawSubMode === 'task'
                        ? activeBot?.hierarchical_memory?.assembled_task_prompt || activeBot?.assembled_prompt
                        : activeBot?.assembled_prompt) || '暂无装配提示词'
                    }
                    className="text-slate-800 dark:text-slate-200 text-xs"
                  />
                </div>
              </div>
            )}
          </div>
        )}

        {/* 视图 2: 基础设定 (Base Persona) */}
        {promptViewMode === 'base' && (
          <div className="space-y-2.5">
            {activeBot?.id === 'default_assistant' || !canEditDefinition ? (
              /* 内置智能体：受保护，严禁修改 */
              <div className="space-y-2.5">
                <div className="p-3 rounded-xl bg-amber-500/10 border border-amber-500/20 text-[11px] text-amber-900 dark:text-amber-200 leading-relaxed flex items-start gap-2.5">
                  <Lock className="h-4 w-4 text-amber-600 dark:text-amber-400 shrink-0 mt-0.5" />
                  <div className="space-y-1">
                    <div className="font-bold flex items-center gap-1.5">
                      <span>官方核心内置设定（系统保护 · 只读）</span>
                    </div>
                    <p className="text-[10px] text-slate-600 dark:text-slate-400 leading-normal">
                      助手共享人设由平台或对应范围的管理员维护。公共 BOT 对话中的偏好、记忆和技能仅属于当前用户；也可以新建私有 BOT 定制人设。云接口调用仍受当前用户角色及高危操作确认约束。
                    </p>
                  </div>
                </div>

                {/* 编辑器视窗 */}
                <div className="rounded-xl border border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-950 overflow-hidden shadow-2xs">
                  <div className="flex items-center justify-between px-3.5 py-2 bg-slate-100/90 dark:bg-slate-900 border-b border-slate-200 dark:border-slate-800 text-xs font-mono select-none">
                    <div className="flex items-center gap-2 text-slate-600 dark:text-slate-400">
                      <FileText className="h-3.5 w-3.5 text-blue-500" />
                      <span className="font-semibold text-slate-700 dark:text-slate-300">base_persona.md</span>
                      <Badge variant="outline" className="text-[10px] py-0 px-1 border-amber-300 dark:border-amber-700 text-amber-600">只读保护</Badge>
                      <span className="text-[11px] text-slate-400 font-normal">({activeBot?.base_prompt?.length || 0} 字)</span>
                    </div>
                    <button
                      type="button"
                      onClick={() => {
                        if (activeBot?.base_prompt) {
                          navigator.clipboard.writeText(activeBot.base_prompt)
                          setCopiedPromptId('base-persona')
                          setTimeout(() => setCopiedPromptId(null), 2000)
                          showToast({ type: 'success', message: '已复制基础人设设定' })
                        }
                      }}
                      className="flex items-center gap-1 px-2.5 py-1 rounded-md hover:bg-slate-200 dark:hover:bg-slate-800 text-slate-500 hover:text-slate-800 dark:text-slate-400 dark:hover:text-slate-200 transition-colors cursor-pointer text-xs"
                      title="复制内容"
                    >
                      {copiedPromptId === 'base-persona' ? (
                        <>
                          <Check className="h-3 w-3 text-emerald-500" />
                          <span className="text-emerald-600 font-semibold text-[11px]">已复制</span>
                        </>
                      ) : (
                        <>
                          <Copy className="h-3 w-3 text-slate-400" />
                          <span className="text-[11px]">复制</span>
                        </>
                      )}
                    </button>
                  </div>
                  <div className="p-3.5 max-h-[440px] overflow-y-auto font-mono text-xs leading-relaxed text-slate-800 dark:text-slate-200">
                    <MarkdownView
                      content={activeBot?.base_prompt || '暂无基础设定'}
                      className="text-slate-800 dark:text-slate-200 text-xs leading-relaxed"
                    />
                  </div>
                </div>
              </div>
            ) : (
              /* 自定义智能体：允许自由修改与微调 */
              <div className="space-y-2.5">
                <div className="p-2.5 rounded-xl bg-slate-100 dark:bg-slate-800/60 text-[11px] text-slate-600 dark:text-slate-400 leading-relaxed">
                  📝 <strong>人设基底</strong>：修改后保存，实时组装提示词将自动同步重构生效。
                </div>

                {isEditingBasePrompt ? (
                  <div className="space-y-2">
                    <textarea
                      value={editedBasePrompt}
                      onChange={(e) => setEditedBasePrompt(e.target.value)}
                      rows={13}
                      placeholder="输入智能体基础系统设定提示词..."
                      className="w-full p-3 rounded-xl border border-blue-300 dark:border-blue-700 bg-white dark:bg-slate-950 text-xs font-mono text-slate-800 dark:text-slate-200 focus:outline-hidden leading-relaxed resize-y"
                    />
                    <div className="flex items-center justify-between text-[10px] text-slate-400">
                      <span>字数: {editedBasePrompt.length}</span>
                      <div className="flex items-center gap-2">
                        <Button
                          variant="outline"
                          size="sm"
                          onClick={() => {
                            setEditedBasePrompt(activeBot?.base_prompt || '')
                            setIsEditingBasePrompt(false)
                          }}
                          disabled={isSavingBasePrompt}
                          className="h-7 text-xs cursor-pointer"
                        >
                          取消
                        </Button>
                        <Button
                          size="sm"
                          onClick={async () => {
                            await onSaveBasePrompt(editedBasePrompt)
                            setIsEditingBasePrompt(false)
                          }}
                          disabled={isSavingBasePrompt || !editedBasePrompt.trim()}
                          className="h-7 text-xs bg-blue-600 hover:bg-blue-500 text-white cursor-pointer"
                        >
                          {isSavingBasePrompt ? '保存中...' : '保存修改'}
                        </Button>
                      </div>
                    </div>
                  </div>
                ) : (
                  /* 编辑器视窗 */
                  <div className="rounded-xl border border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-950 overflow-hidden shadow-2xs">
                    <div className="flex items-center justify-between px-3.5 py-2 bg-slate-100/90 dark:bg-slate-900 border-b border-slate-200 dark:border-slate-800 text-xs font-mono select-none">
                      <div className="flex items-center gap-2 text-slate-600 dark:text-slate-400">
                        <FileText className="h-3.5 w-3.5 text-blue-500" />
                        <span className="font-semibold text-slate-700 dark:text-slate-300">base_persona.md</span>
                        <span className="text-[11px] text-slate-400 font-normal">({activeBot?.base_prompt?.length || 0} 字)</span>
                      </div>
                      <div className="flex items-center gap-1.5">
                        <button
                          type="button"
                          onClick={() => {
                            if (activeBot?.base_prompt) {
                              navigator.clipboard.writeText(activeBot.base_prompt)
                              setCopiedPromptId('base-custom')
                              setTimeout(() => setCopiedPromptId(null), 2000)
                              showToast({ type: 'success', message: '已复制基础人设设定' })
                            }
                          }}
                          className="flex items-center gap-1 px-2.5 py-1 rounded-md hover:bg-slate-200 dark:hover:bg-slate-800 text-slate-500 hover:text-slate-800 dark:text-slate-400 dark:hover:text-slate-200 transition-colors cursor-pointer text-xs"
                          title="复制内容"
                        >
                          {copiedPromptId === 'base-custom' ? (
                            <>
                              <Check className="h-3.5 w-3.5 text-emerald-500" />
                              <span className="text-emerald-600 font-semibold text-[11px]">已复制</span>
                            </>
                          ) : (
                            <>
                              <Copy className="h-3.5 w-3.5 text-slate-400" />
                              <span className="text-[11px]">复制</span>
                            </>
                          )}
                        </button>
                        <button
                          type="button"
                          onClick={() => {
                            setEditedBasePrompt(activeBot?.base_prompt || '')
                            setIsEditingBasePrompt(true)
                          }}
                          className="flex items-center gap-1 text-xs px-2.5 py-1 rounded-md text-blue-600 dark:text-blue-400 hover:bg-blue-50 dark:hover:bg-blue-950/40 cursor-pointer font-semibold transition-colors"
                        >
                          <Edit3 className="h-3 w-3" />
                          <span>修改设定</span>
                        </button>
                      </div>
                    </div>
                    <div className="p-3.5 max-h-[440px] overflow-y-auto font-mono text-xs leading-relaxed text-slate-800 dark:text-slate-200">
                      <MarkdownView
                        content={activeBot?.base_prompt || '暂无基础设定'}
                        className="text-slate-800 dark:text-slate-200 text-xs leading-relaxed"
                      />
                    </div>
                  </div>
                )}
              </div>
            )}
          </div>
        )}
      </div>

      {/* 智能体生命周期与危险区 (删除智能体) */}
      <div className="pt-3 border-t border-slate-200 dark:border-slate-800">
        <div className="p-3.5 rounded-2xl border border-rose-200/80 dark:border-rose-900/50 bg-rose-50/40 dark:bg-rose-950/20 flex items-center justify-between gap-3">
          <div>
            <div className="text-xs font-bold text-rose-700 dark:text-rose-400 flex items-center gap-1.5">
              <Trash2 className="h-3.5 w-3.5 text-rose-600" />
              <span>删除当前智能体</span>
            </div>
            <div className="text-[11px] text-slate-500 dark:text-slate-400 mt-1 leading-relaxed">
              {activeBot?.id === 'default_assistant'
                ? '默认助手受系统内置保护，不可删除。'
                : `永久移除「${activeBot?.name}」，清理其所有记忆、技能与历史会话。`}
            </div>
          </div>
          {activeBot?.id !== 'default_assistant' && canEditDefinition ? (
            <Button
              variant="destructive"
              size="sm"
              onClick={onDeleteActiveBot}
              className="bg-rose-600 hover:bg-rose-500 text-white text-xs font-medium cursor-pointer shrink-0 shadow-xs"
            >
              <Trash2 className="h-3 w-3 mr-1" />
              <span>删除智能体</span>
            </Button>
          ) : (
            <Badge variant="outline" className="text-[10px] text-slate-400 border-slate-200 dark:border-slate-800 shrink-0">
              系统受保护
            </Badge>
          )}
        </div>
      </div>
    </div>
  )
}
