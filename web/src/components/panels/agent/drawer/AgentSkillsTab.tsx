import React from 'react'
import { Code2, Trash2 } from 'lucide-react'
import { SkillItem } from './types'

interface AgentSkillsTabProps {
  skills: SkillItem[]
  isPublicBot?: boolean
  onInspectSkill: (skill: SkillItem) => void
  onDeleteSkill: (skillName: string) => void
  onRunSkill: (skillName: string) => void
}

export const AgentSkillsTab: React.FC<AgentSkillsTabProps> = ({
  skills,
  isPublicBot = false,
  onInspectSkill,
  onDeleteSkill,
  onRunSkill,
}) => {
  return (
    <div className="space-y-3">
      <div className="p-2.5 rounded-xl bg-amber-50 dark:bg-amber-950/40 border border-amber-200 dark:border-amber-800 text-[11px] text-amber-800 dark:text-amber-300 leading-relaxed">
        {isPublicBot ? '我的技能：仅当前用户可见、修改和调用；同名技能不会影响其他用户。' : '专属技能：智能体可通过 save_skill 保存已验证的流程，供后续任务复用。'}
      </div>

      {(!skills || skills.length === 0) ? (
        <div className="text-center py-8 text-slate-400 text-xs">
          当前智能体尚未沉淀自编技能。<br />
          当它执行复杂批量任务后，会自动提炼经验并沉淀技能。
        </div>
      ) : (
        skills.map((s: SkillItem) => (
          <div
            key={s.name}
            className="p-3 rounded-xl border border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900 shadow-2xs relative group"
          >
            <div className="flex items-center justify-between gap-1 mb-1">
              <span className="font-bold font-mono text-blue-600 dark:text-blue-400 text-xs">
                {s.name}
              </span>
              <div className="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
                <button
                  type="button"
                  onClick={() => onInspectSkill(s)}
                  className="text-slate-400 hover:text-blue-500 p-0.5 rounded cursor-pointer"
                  title="查看脚本代码"
                >
                  <Code2 className="h-3 w-3" />
                </button>
                <button
                  type="button"
                  onClick={() => onDeleteSkill(s.name)}
                  className="text-slate-400 hover:text-rose-500 p-0.5 rounded cursor-pointer"
                  title="删除技能"
                >
                  <Trash2 className="h-3 w-3" />
                </button>
              </div>
            </div>
            <p className="text-slate-600 dark:text-slate-400 text-[11px] line-clamp-2 mb-2">
              {s.description || '无详细描述'}
            </p>
            <div className="flex items-center justify-between text-[10px] text-slate-400">
              <span>创建: {s.created_at?.slice(0, 10)}</span>
              <div className="flex items-center gap-1.5">
                <button
                  type="button"
                  onClick={() => onInspectSkill(s)}
                  className="px-1.5 py-0.5 rounded border border-slate-200 dark:border-slate-700 text-slate-600 dark:text-slate-300 hover:bg-slate-50 dark:hover:bg-slate-800 text-[10px] cursor-pointer"
                >
                  源码
                </button>
                <button
                  type="button"
                  onClick={() => onRunSkill(s.name)}
                  className="px-2 py-0.5 rounded bg-blue-50 dark:bg-blue-950 text-blue-600 dark:text-blue-400 hover:bg-blue-100 font-semibold cursor-pointer text-[10px]"
                >
                  运行 ▶
                </button>
              </div>
            </div>
          </div>
        ))
      )}
    </div>
  )
}
