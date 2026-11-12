import React from 'react'
import {
  BookOpen,
  Loader2,
  Plus,
  Download,
  Trash2,
  Search,
} from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { KnowledgeDoc } from './types'

interface AgentKnowledgeTabProps {
  activeBotId: string
  isPublicBot?: boolean
  knowledgeDocs: KnowledgeDoc[]
  isLoadingKnowledge: boolean
  isUploadingKnowledge: boolean
  isSearchingKnowledge: boolean
  knowledgeSearchQuery: string
  setKnowledgeSearchQuery: (q: string) => void
  knowledgeSearchResults: any[]
  hasSearchedKnowledge: boolean
  knowledgeFileInputRef: React.RefObject<HTMLInputElement | null>
  onUploadKnowledge: (e: React.ChangeEvent<HTMLInputElement>) => void
  onToggleKnowledgeDoc: (docId: string, currentStatus: boolean) => void
  onDeleteKnowledgeDoc: (docId: string) => void
  onTestSearchKnowledge: () => void
}

export const AgentKnowledgeTab: React.FC<AgentKnowledgeTabProps> = ({
  activeBotId,
  isPublicBot = false,
  knowledgeDocs,
  isLoadingKnowledge,
  isUploadingKnowledge,
  isSearchingKnowledge,
  knowledgeSearchQuery,
  setKnowledgeSearchQuery,
  knowledgeSearchResults,
  hasSearchedKnowledge,
  knowledgeFileInputRef,
  onUploadKnowledge,
  onToggleKnowledgeDoc,
  onDeleteKnowledgeDoc,
  onTestSearchKnowledge,
}) => {
  return (
    <div className="space-y-4">
      {/* 说明 Banner */}
      <div className="p-3 rounded-xl bg-gradient-to-r from-sky-50 to-indigo-50/70 dark:from-sky-950/40 dark:to-indigo-950/30 border border-sky-200/80 dark:border-sky-800/80 text-[11px] text-sky-900 dark:text-sky-200 leading-relaxed shadow-2xs">
        <div className="flex items-center gap-1.5 font-semibold text-xs text-sky-800 dark:text-sky-200 mb-1">
          <BookOpen className="h-3.5 w-3.5 text-sky-500" />
          <span>{isPublicBot ? '我的知识库' : '企业知识库与私有运维手册'} (Lightweight RAG)</span>
        </div>
        为当前智能体挂载团队特有的故障排障手册、业务规范、敏感词规则或私有文档（支持 Markdown、Word、PDF、TXT、JSON、CSV）。智能体在执行需求时会自动结合语义检索召回规范依据，并在需要时调用{' '}
        <code className="px-1 py-0.2 rounded bg-sky-100 dark:bg-sky-900 font-mono text-[10px]">
          search_knowledge
        </code>{' '}
        工具。
        {isPublicBot && ' 公共 BOT 共享助手能力；这里的文档只用于当前用户的会话，其他用户无法查看、下载或检索。'}
      </div>

      {/* 隐藏文件输入控件 */}
      <input
        type="file"
        ref={knowledgeFileInputRef}
        onChange={onUploadKnowledge}
        accept=".md,.txt,.docx,.pdf,.json,.csv,.tsv,.log"
        className="hidden"
      />

      {/* 工具条：上传按钮与统计 */}
      <div className="flex items-center justify-between gap-2 p-2.5 rounded-xl bg-slate-50 dark:bg-slate-950/60 border border-slate-200 dark:border-slate-800">
        <div className="flex items-center gap-1.5 text-xs text-slate-600 dark:text-slate-400 font-medium">
          <span>共 {knowledgeDocs.length} 份手册</span>
          <span className="text-slate-300 dark:text-slate-700">|</span>
          <span className="text-emerald-600 dark:text-emerald-400 font-mono text-[11px]">
            {knowledgeDocs.filter((d) => d.enabled).length} 篇已启用检索
          </span>
          {isLoadingKnowledge && <Loader2 className="h-3 w-3 animate-spin text-blue-500" />}
        </div>

        <Button
          size="sm"
          onClick={() => knowledgeFileInputRef.current?.click()}
          disabled={isUploadingKnowledge}
          className="h-7 text-xs bg-sky-600 hover:bg-sky-500 text-white gap-1 cursor-pointer"
        >
          {isUploadingKnowledge ? (
            <>
              <Loader2 className="h-3 w-3 animate-spin" />
              <span>正在切片索引...</span>
            </>
          ) : (
            <>
              <Plus className="h-3 w-3" />
              <span>上传规范手册</span>
            </>
          )}
        </Button>
      </div>

      {/* 文档列表卡片 */}
      {knowledgeDocs.length === 0 ? (
        <div className="text-center py-8 text-slate-400 text-xs space-y-2 border border-dashed border-slate-200 dark:border-slate-800 rounded-2xl p-6">
          <BookOpen className="h-9 w-9 mx-auto text-slate-300 dark:text-slate-600 stroke-[1.5]" />
          <div className="font-semibold text-slate-600 dark:text-slate-300 text-sm">暂未挂载任何知识库文档</div>
          <div className="text-[11px] text-slate-400 max-w-sm mx-auto leading-relaxed">
            点击上方“上传规范手册”，上传团队自研 IM 业务文档、错误排查 SOP 或合规准则。系统将自动执行文本切片、Token 索引并构建本地 BM25 检索知识库。
          </div>
        </div>
      ) : (
        <div className="space-y-2.5">
          {knowledgeDocs.map((doc) => (
            <div
              key={doc.id}
              className={`p-3 rounded-xl border transition-all duration-200 ${
                doc.enabled
                  ? 'border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900 shadow-2xs hover:border-sky-300 dark:hover:border-sky-700'
                  : 'border-slate-200/50 dark:border-slate-800/40 bg-slate-50/60 dark:bg-slate-950/40 opacity-70'
              }`}
            >
              <div className="flex items-start justify-between gap-2.5">
                <div className="flex items-start gap-2.5 min-w-0 flex-1">
                  <div className="w-8 h-8 rounded-lg bg-sky-50 dark:bg-sky-950/70 border border-sky-100 dark:border-sky-900 flex items-center justify-center shrink-0 mt-0.5 text-sky-600 dark:text-sky-400">
                    <BookOpen className="h-4 w-4" />
                  </div>
                  <div className="min-w-0 flex-1">
                    <div className="text-xs font-semibold text-slate-800 dark:text-slate-200 truncate flex items-center gap-1.5" title={doc.name}>
                      <span className="truncate">{doc.name}</span>
                      <Badge variant="outline" className="text-[9px] px-1 py-0 font-mono uppercase shrink-0">
                        {doc.file_type}
                      </Badge>
                    </div>
                    <div className="text-[10px] text-slate-400 flex items-center gap-2 mt-1 font-mono">
                      <span>{doc.size_formatted}</span>
                      <span>·</span>
                      <span className="text-sky-600 dark:text-sky-400 font-semibold">{doc.chunks_count} 个切片</span>
                      <span>·</span>
                      <span>{doc.created_at?.slice(0, 16)}</span>
                    </div>
                    {doc.preview && (
                      <div className="text-[11px] text-slate-500 dark:text-slate-400 mt-1.5 line-clamp-2 leading-relaxed bg-slate-50 dark:bg-slate-950/60 p-1.5 rounded-md border border-slate-100 dark:border-slate-800/60 font-mono">
                        {doc.preview}
                      </div>
                    )}
                  </div>
                </div>

                {/* 操作按钮区 */}
                <div className="flex items-center gap-1 shrink-0 pt-0.5">
                  <button
                    type="button"
                    onClick={() => onToggleKnowledgeDoc(doc.id, doc.enabled)}
                    className={`px-2 py-0.5 rounded-full text-[10px] font-medium transition-colors cursor-pointer border ${
                      doc.enabled
                        ? 'bg-emerald-50 dark:bg-emerald-950/60 border-emerald-300 dark:border-emerald-700 text-emerald-700 dark:text-emerald-300'
                        : 'bg-slate-100 dark:bg-slate-800 border-slate-300 dark:border-slate-700 text-slate-500'
                    }`}
                    title={doc.enabled ? '点击禁用该文档参与检索' : '点击启用该文档参与检索'}
                  >
                    {doc.enabled ? '已启用' : '已停用'}
                  </button>

                  <a
                    href={`/api/agent/bots/${activeBotId}/knowledge/docs/${doc.id}/download`}
                    download={doc.name}
                    target="_blank"
                    rel="noreferrer"
                    className="p-1 rounded-lg hover:bg-sky-50 dark:hover:bg-sky-950/60 text-slate-400 hover:text-sky-600 transition-colors"
                    title="下载原始文件"
                  >
                    <Download className="h-3.5 w-3.5" />
                  </a>

                  <button
                    type="button"
                    onClick={() => onDeleteKnowledgeDoc(doc.id)}
                    className="p-1 rounded-lg hover:bg-rose-50 dark:hover:bg-rose-950/60 text-slate-400 hover:text-rose-600 transition-colors cursor-pointer"
                    title="删除该文档与全部索引切片"
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </button>
                </div>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* 知识检索与召回实时调试沙盘 */}
      <div className="mt-4 p-3 rounded-2xl border border-slate-200 dark:border-slate-800 bg-slate-50/70 dark:bg-slate-950/40 space-y-2.5">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-1.5 text-xs font-semibold text-slate-700 dark:text-slate-300">
            <Search className="h-3.5 w-3.5 text-sky-500" />
            <span>知识检索与召回实时调试 (Search Playground)</span>
          </div>
          <span className="text-[10px] text-slate-400 font-mono">BM25 算法</span>
        </div>

        <div className="flex items-center gap-2">
          <input
            type="text"
            value={knowledgeSearchQuery}
            onChange={(e) => setKnowledgeSearchQuery(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && onTestSearchKnowledge()}
            placeholder="输入问题或关键词测试召回（如 '10007', '解散群规范', '禁言流程'）..."
            className="flex-1 h-8 px-3 rounded-lg border border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900 text-xs text-slate-800 dark:text-slate-200 placeholder:text-slate-400 focus:outline-hidden focus:border-sky-500"
          />
          <Button
            size="sm"
            onClick={onTestSearchKnowledge}
            disabled={isSearchingKnowledge || !knowledgeSearchQuery.trim()}
            className="h-8 px-3 text-xs bg-sky-600 hover:bg-sky-500 text-white cursor-pointer shrink-0"
          >
            {isSearchingKnowledge ? <Loader2 className="h-3 w-3 animate-spin" /> : '测试召回'}
          </Button>
        </div>

        {/* 搜索测试结果 */}
        {hasSearchedKnowledge && (
          <div className="space-y-2 pt-1 border-t border-slate-200 dark:border-slate-800/80">
            <div className="text-[11px] text-slate-500 flex items-center justify-between">
              <span>命中文档切片 ({knowledgeSearchResults.length} 条)</span>
              {knowledgeSearchResults.length > 0 && (
                <span className="text-emerald-600 dark:text-emerald-400 font-mono text-[10px]">
                  最高相关度: {knowledgeSearchResults[0]?.score}
                </span>
              )}
            </div>

            {knowledgeSearchResults.length === 0 ? (
              <div className="p-3 text-center text-slate-400 text-xs">
                未匹配到相关文档切片。请检查关键词或尝试上传包含该知识的手册文档。
              </div>
            ) : (
              knowledgeSearchResults.map((res: any, idx: number) => (
                <div
                  key={idx}
                  className="p-2.5 rounded-lg border border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900 text-xs space-y-1"
                >
                  <div className="flex items-center justify-between font-semibold text-slate-700 dark:text-slate-300">
                    <span className="truncate max-w-[240px]">
                      《{res.doc_name}》- {res.title}
                    </span>
                    <Badge className="bg-sky-100 dark:bg-sky-950 text-sky-700 dark:text-sky-300 border-sky-300 font-mono text-[10px] h-4 px-1.5">
                      Score: {res.score}
                    </Badge>
                  </div>
                  <div className="p-2 rounded bg-slate-50 dark:bg-slate-950 text-[11px] font-mono text-slate-600 dark:text-slate-400 whitespace-pre-wrap leading-relaxed max-h-36 overflow-y-auto">
                    {res.content}
                  </div>
                </div>
              ))
            )}
          </div>
        )}
      </div>
    </div>
  )
}
