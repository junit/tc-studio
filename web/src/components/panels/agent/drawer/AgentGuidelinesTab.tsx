import React from 'react'
import { Trash2 } from 'lucide-react'
import { LearnedGuideline } from './types'

interface AgentGuidelinesTabProps {
  guidelines: LearnedGuideline[]
  isPublicBot?: boolean
  canEdit: boolean
  onDeleteGuideline: (id: string) => void
}

export const AgentGuidelinesTab: React.FC<AgentGuidelinesTabProps> = ({
  guidelines,
  isPublicBot = false,
  canEdit,
  onDeleteGuideline,
}) => {
  return (
    <div className="space-y-3">
      <div className="p-2.5 rounded-xl bg-blue-50 dark:bg-blue-950/40 border border-blue-200 dark:border-blue-800 text-[11px] text-blue-800 dark:text-blue-300 leading-relaxed">
        {isPublicBot
          ? '这里是所有用户共用的助手准则，由对应范围的管理员显式管理。对话中自动学习的个人偏好保存在“记忆”中，仅自己可见。'
          : '智能体可通过 enhance_self_prompt 沉淀专属行为准则，供该私有 BOT 后续任务使用。'}
      </div>

      {(!guidelines || guidelines.length === 0) ? (
        <div className="text-center py-8 text-slate-400 text-xs">
          暂无沉淀的强化准则。<br />
          {isPublicBot ? '个人偏好请在“记忆”中查看。' : '可在日常对话中表达您的长期要求。'}
        </div>
      ) : (
        guidelines.map((g: LearnedGuideline, idx: number) => (
          <div
            key={g.id || idx}
            className="p-3 rounded-xl border border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900 shadow-2xs relative group"
          >
            <div className="flex items-start justify-between gap-2 mb-1">
              <span className="font-semibold text-slate-900 dark:text-slate-100 text-xs">
                准则 #{idx + 1}
              </span>
              <button
                disabled={!canEdit}
                onClick={() => onDeleteGuideline(g.id)}
                className="text-slate-400 hover:text-rose-500 p-0.5 rounded cursor-pointer opacity-0 group-hover:opacity-100 transition-opacity"
                title="删除该准则"
              >
                <Trash2 className="h-3 w-3" />
              </button>
            </div>
            <p className="text-slate-700 dark:text-slate-300 leading-relaxed text-[11px] mb-1.5">
              {g.instruction}
            </p>
            {g.reason && (
              <div className="text-[10px] text-slate-400 italic">
                💡 触发原因: {g.reason}
              </div>
            )}
            {g.created_at && (
              <div className="text-[9px] text-slate-400 mt-1 font-mono">
                🕒 {g.created_at.slice(0, 19).replace('T', ' ')}
              </div>
            )}
          </div>
        ))
      )}
    </div>
  )
}
