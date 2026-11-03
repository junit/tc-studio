import React, { useState, useEffect, useMemo, useRef } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { Group, Panel, useGroupRef } from 'react-resizable-panels'
import {
  Send,
  ExternalLink,
  Layers,
  RotateCcw,
  Code,
  FileText,
  Copy,
  Check,
  CheckCircle2,
  AlertTriangle,
  Clock,
  Sparkles,
  Terminal,
  FileCode,
  ChevronDown,
  ChevronUp,
  ChevronRight,
  Info,
  Maximize2,
  Minimize2,
  BookOpen,
  Trash2,
  Eye,
  EyeOff,
  ShieldCheck,
  ShieldAlert,
  X,
  Plus,
  Search,
  History,
} from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Badge } from '@/components/ui/badge'
import { ResizeHandle } from '@/components/ui/resize-handle'
import { useApiStore, ApiMeta, ApiTab, ApiField, initializeFormData, smartMatchApi, getCleanApiName } from '@/stores/apiStore'
import { useBatchStore } from '@/stores/batchStore'
import { useConfigStore } from '@/stores/configStore'
import { ApiDocsViewer } from '@/components/panels/ApiDocsViewer'
import { JsonCodeEditor } from '@/components/common/JsonCodeEditor'

export type FieldTypeCategory = 'string' | 'number' | 'boolean' | 'list' | 'json'

export function getFieldTypeCategory(typeStr?: string, val?: any): FieldTypeCategory {
  // If actual runtime value is non-empty, use it as ground truth
  if (val !== undefined && val !== null) {
    if (typeof val === 'boolean') return 'boolean'
    if (typeof val === 'number') return 'number'
    if (Array.isArray(val)) return 'list'
    if (typeof val === 'object') return 'json'
  }

  const raw = (typeStr || '').trim()
  const t = raw.toLowerCase()
  if (t === 'boolean' || t === 'bool') return 'boolean'
  if (t.includes('array') || t.includes('list') || raw.endsWith('[]')) return 'list'
  if (
    t === 'number' ||
    t === 'int' ||
    t === 'integer' ||
    t === 'float' ||
    t === 'double' ||
    t.includes('int64') ||
    t.includes('uint')
  )
    return 'number'
  if (
    t === 'json' ||
    t === 'object' ||
    t === 'dict' ||
    t === 'map' ||
    (raw.length > 0 &&
      raw[0] === raw[0].toUpperCase() &&
      raw !== 'String' &&
      raw !== 'Integer' &&
      raw !== 'Float' &&
      raw !== 'Boolean')
  )
    return 'json'
  return 'string'
}

export function isComplexListField(field: ApiField, val: any): boolean {
  if (Array.isArray(val) && val.some((x) => typeof x === 'object' && x !== null))
    return true
  if (
    Array.isArray(field.default) &&
    field.default.some((x: any) => typeof x === 'object' && x !== null)
  )
    return true
  const rawType = (field.type || '').toLowerCase()
  if (
    rawType.includes('object') ||
    rawType.includes('param') ||
    rawType.includes('info') ||
    rawType.includes('item')
  )
    return true
  if (
    field.name === 'MsgBody' ||
    field.name === 'C2CMsgInfo' ||
    field.name === 'MemberList' ||
    field.name === 'CheckItem' ||
    field.name === 'DeleteItem' ||
    field.name === 'MsgSeqList' ||
    field.name === 'ExtensionList' ||
    field.name === 'AddFriendItem' ||
    field.name === 'UserTags' ||
    field.name === 'ProfileItem' ||
    field.name === 'InputStreamList' ||
    field.name === 'InputList' ||
    field.name === 'AdaptiveChildren' ||
    field.name === 'DomainInfos' ||
    field.name === 'Images' ||
    field.name === 'Keywords' ||
    field.name === 'ImageInfos' ||
    field.name === 'VideoInfos'
  )
    return true
  if (field.placeholder?.includes('JSON 数组')) return true
  return false
}

export function sanitizePayloadTypes(
  rawPayload: Record<string, any>,
  fields?: ApiField[]
): Record<string, any> {
  if (!rawPayload || typeof rawPayload !== 'object') return {}
  const result: Record<string, any> = { ...rawPayload }
  const fieldMap = new Map<string, ApiField>()
  if (fields) {
    for (const f of fields) {
      fieldMap.set(f.name, f)
    }
  }

  for (const [key, val] of Object.entries(result)) {
    const field = fieldMap.get(key)
    const category = field
      ? getFieldTypeCategory(field.type, val)
      : getFieldTypeCategory(undefined, val)

    if (category === 'number') {
      if (val === '' || val === null || val === undefined) {
        delete result[key]
      } else if (typeof val === 'string') {
        const num = Number(val)
        // JSON/JavaScript numbers cannot represent integer IDs above 2^53-1
        // exactly. Keep those values as strings so MsgSeq/timestamps are sent
        // losslessly to the API.
        const looksLikeInteger = /^[+-]?\d+$/.test(val.trim())
        if (!isNaN(num) && !(looksLikeInteger && !Number.isSafeInteger(num))) {
          result[key] = num
        }
      }
    } else if (category === 'boolean') {
      if (typeof val === 'string') {
        result[key] = val.toLowerCase() === 'true' || val === '1'
      } else {
        result[key] = Boolean(val)
      }
    } else if (category === 'list') {
      let listVal: any[] = []
      if (typeof val === 'string') {
        const trimmed = val.trim()
        if (trimmed.startsWith('[') && trimmed.endsWith(']')) {
          try {
            listVal = JSON.parse(trimmed)
          } catch {
            listVal = trimmed
              .slice(1, -1)
              .split(/[,，]/)
              .map((s) => s.trim())
              .filter(Boolean)
          }
        } else if (trimmed === '') {
          listVal = []
        } else {
          listVal = trimmed
            .split(/[,，]/)
            .map((s) => s.trim())
            .filter(Boolean)
        }
      } else if (Array.isArray(val)) {
        listVal = val
      } else if (val !== undefined && val !== null) {
        listVal = [val]
      }

      // Recursively sanitize objects inside list (parse stringified nested objects)
      result[key] = listVal.map((item) => {
        if (typeof item === 'object' && item !== null) {
          const cleanedItem: Record<string, any> = { ...item }
          for (const [subKey, subVal] of Object.entries(cleanedItem)) {
            if (typeof subVal === 'string') {
              const subTrim = subVal.trim()
              if (
                (subTrim.startsWith('{') && subTrim.endsWith('}')) ||
                (subTrim.startsWith('[') && subTrim.endsWith(']'))
              ) {
                try {
                  cleanedItem[subKey] = JSON.parse(subTrim)
                } catch {}
              }
            }
          }
          return cleanedItem
        }
        return item
      })
    } else if (category === 'json') {
      if (typeof val === 'string') {
        const trimmed = val.trim()
        if (trimmed === '' || trimmed === '{}') {
          result[key] = {}
        } else {
          try {
            result[key] = JSON.parse(trimmed)
          } catch {
            // Keep original string if cannot parse
          }
        }
      } else if (val === null || val === undefined) {
        delete result[key]
      }
    } else if (category === 'string') {
      if (val !== undefined && val !== null && typeof val !== 'string') {
        // CRITICAL: Never turn an object into "[object Object]"
        if (typeof val === 'object') {
          result[key] = val
        } else {
          result[key] = String(val)
        }
      }
    }
  }
  return result
}

interface NestedObjectEditorProps {
  parentKey: string
  structName?: string
  subFieldsDef?: ApiField[]
  value: any
  onChange: (val: any) => void
}

const NestedObjectEditor: React.FC<NestedObjectEditorProps> = ({
  parentKey,
  structName,
  subFieldsDef,
  value,
  onChange,
}) => {
  const [mode, setMode] = useState<'form' | 'json'>('form')
  const [newPropKey, setNewPropKey] = useState('')
  const [showAddProp, setShowAddProp] = useState(false)
  const [rawText, setRawText] = useState(() => {
    try {
      return typeof value === 'object' && value !== null
        ? JSON.stringify(value, null, 2)
        : String(value || '{}')
    } catch {
      return '{}'
    }
  })
  const [error, setError] = useState<string | null>(null)

  // 规范化对象值
  const objValue: Record<string, any> = useMemo(() => {
    if (typeof value === 'object' && value !== null && !Array.isArray(value)) {
      return value
    }
    if (typeof value === 'string' && value.trim()) {
      try {
        const p = JSON.parse(value.trim())
        if (typeof p === 'object' && p !== null && !Array.isArray(p)) return p
      } catch {}
    }
    return {}
  }, [value])

  const subFields = subFieldsDef || []
  const subFieldMap = useMemo(() => {
    const map = new Map<string, ApiField>()
    subFields.forEach((sf) => map.set(sf.name, sf))
    return map
  }, [subFields])

  const displayKeys = useMemo(() => {
    const keys = new Set<string>()
    subFields.forEach((sf) => keys.add(sf.name))
    Object.keys(objValue).forEach((k) => keys.add(k))
    return Array.from(keys)
  }, [subFields, objValue])

  useEffect(() => {
    if (mode === 'form') {
      try {
        setRawText(JSON.stringify(objValue, null, 2))
        setError(null)
      } catch {}
    }
  }, [objValue, mode])

  const handleSubFieldChange = (subKey: string, rawVal: string) => {
    const sf = subFieldMap.get(subKey)
    const category = sf
      ? getFieldTypeCategory(sf.type, rawVal)
      : getFieldTypeCategory(undefined, rawVal)

    let finalVal: any = rawVal
    if (category === 'number') {
      if (rawVal === '') {
        finalVal = undefined
      } else {
        const num = Number(rawVal)
        finalVal = isNaN(num) ? rawVal : num
      }
    } else if (category === 'boolean') {
      finalVal = rawVal === 'true'
    }

    const next = { ...objValue }
    if (finalVal === undefined) {
      delete next[subKey]
    } else {
      next[subKey] = finalVal
    }
    onChange(next)
  }

  const handleDeleteSubKey = (subKey: string) => {
    const next = { ...objValue }
    delete next[subKey]
    onChange(next)
  }

  const handleAddSubProperty = () => {
    const k = newPropKey.trim()
    if (!k) return
    const next = { ...objValue, [k]: '' }
    onChange(next)
    setNewPropKey('')
    setShowAddProp(false)
  }

  const handleRawChange = (text: string) => {
    setRawText(text)
    try {
      const parsed = JSON.parse(text)
      if (typeof parsed === 'object' && parsed !== null && !Array.isArray(parsed)) {
        setError(null)
        onChange(parsed)
      } else {
        setError('必须是 JSON 对象格式 (如 {"key": "val"})')
      }
    } catch (e: any) {
      setError(e.message)
    }
  }

  return (
    <div className="flex flex-col gap-2 rounded-lg border border-purple-200/80 dark:border-purple-900/50 bg-purple-50/20 dark:bg-purple-950/20 p-2.5">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-1.5">
          <span className="text-[11px] font-mono font-semibold text-purple-700 dark:text-purple-300 flex items-center gap-1">
            <span className="text-[9px] bg-purple-100 dark:bg-purple-900/60 text-purple-700 dark:text-purple-300 px-1 py-0.2 rounded font-mono font-bold">
              &#123; &#125;
            </span>
            {structName ? `${parentKey} (${structName})` : parentKey}
          </span>
          <span className="text-[10px] text-slate-400 dark:text-slate-500">
            ({displayKeys.length} 个参数)
          </span>
        </div>

        <div className="flex items-center gap-2">
          {mode === 'form' && (
            <button
              type="button"
              onClick={() => setShowAddProp(!showAddProp)}
              className="text-[10px] text-purple-600 dark:text-purple-400 hover:text-purple-700 hover:underline flex items-center gap-0.5 cursor-pointer"
            >
              <Plus className="w-3 h-3" />
              添加属性
            </button>
          )}
          <div className="flex items-center bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded p-0.5 text-[9px]">
            <button
              type="button"
              onClick={() => setMode('form')}
              className={`px-1.5 py-0.5 rounded transition-all cursor-pointer ${
                mode === 'form'
                  ? 'bg-purple-600 text-white font-semibold'
                  : 'text-slate-500 hover:text-slate-800 dark:text-slate-400 dark:hover:text-slate-200'
              }`}
            >
              表单
            </button>
            <button
              type="button"
              onClick={() => setMode('json')}
              className={`px-1.5 py-0.5 rounded transition-all cursor-pointer ${
                mode === 'json'
                  ? 'bg-purple-600 text-white font-semibold'
                  : 'text-slate-500 hover:text-slate-800 dark:text-slate-400 dark:hover:text-slate-200'
              }`}
            >
              JSON
            </button>
          </div>
        </div>
      </div>

      {showAddProp && (
        <div className="flex items-center gap-1.5 p-1.5 bg-white dark:bg-slate-850 border border-purple-200 dark:border-purple-800 rounded-md">
          <Input
            value={newPropKey}
            onChange={(e) => setNewPropKey(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                e.preventDefault()
                handleAddSubProperty()
              }
            }}
            placeholder="输入属性名 (例如 WatermarkId)"
            className="h-6 text-xs font-mono bg-slate-50 dark:bg-slate-900 border-slate-200 dark:border-slate-700"
            autoFocus
          />
          <Button
            size="sm"
            onClick={handleAddSubProperty}
            className="h-6 text-[10px] bg-purple-600 hover:bg-purple-500 text-white px-2 cursor-pointer"
          >
            确定
          </Button>
          <Button
            size="sm"
            variant="ghost"
            onClick={() => {
              setShowAddProp(false)
              setNewPropKey('')
            }}
            className="h-6 text-[10px] px-1.5 text-slate-400 cursor-pointer"
          >
            取消
          </Button>
        </div>
      )}

      {mode === 'form' ? (
        displayKeys.length === 0 ? (
          <div className="py-2 text-center text-xs text-slate-400 dark:text-slate-500 bg-white/60 dark:bg-slate-850/60 rounded border border-dashed border-purple-200 dark:border-purple-900/40">
            暂无属性，点击右上角「添加属性」或切换为 JSON
          </div>
        ) : (
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 bg-white dark:bg-slate-850 p-2 rounded-md border border-slate-200/80 dark:border-slate-800">
            {displayKeys.map((subK) => {
              const sf = subFieldMap.get(subK)
              const curVal = objValue[subK]
              const category = sf
                ? getFieldTypeCategory(sf.type, curVal)
                : getFieldTypeCategory(undefined, curVal)
              const isRequired = sf?.required ?? false

              return (
                <div key={subK} className="flex flex-col gap-1">
                  <div className="flex items-center justify-between">
                    <label className="text-[10px] font-medium text-slate-600 dark:text-slate-300 font-mono flex items-center gap-1">
                      <span>{sf?.label || subK}</span>
                      {isRequired && <span className="text-rose-500 font-bold">*</span>}
                      {sf && sf.label !== subK && (
                        <span className="text-slate-400 font-normal">({subK})</span>
                      )}
                    </label>
                    <div className="flex items-center gap-1">
                      <span className="text-[9px] font-mono px-1 rounded bg-slate-100 dark:bg-slate-800 text-slate-400">
                        {category}
                      </span>
                      {!isRequired && (
                        <button
                          type="button"
                          onClick={() => handleDeleteSubKey(subK)}
                          className="text-slate-300 hover:text-rose-500 p-0.5 rounded transition-colors cursor-pointer"
                          title={`移除属性 ${subK}`}
                        >
                          <Trash2 className="w-2.5 h-2.5" />
                        </button>
                      )}
                    </div>
                  </div>
                  {category === 'boolean' ? (
                    <div className="flex items-center gap-1.5">
                      <button
                        type="button"
                        onClick={() => handleSubFieldChange(subK, 'true')}
                        className={`px-2 py-0.5 text-xs rounded border transition-all ${
                          curVal === true
                            ? 'bg-emerald-600 text-white border-emerald-600 font-semibold'
                            : 'bg-white dark:bg-slate-800 text-slate-600 dark:text-slate-300 border-slate-200 dark:border-slate-700'
                        }`}
                      >
                        True
                      </button>
                      <button
                        type="button"
                        onClick={() => handleSubFieldChange(subK, 'false')}
                        className={`px-2 py-0.5 text-xs rounded border transition-all ${
                          curVal === false
                            ? 'bg-rose-600 text-white border-rose-600 font-semibold'
                            : 'bg-white dark:bg-slate-800 text-slate-600 dark:text-slate-300 border-slate-200 dark:border-slate-700'
                        }`}
                      >
                        False
                      </button>
                    </div>
                  ) : category === 'number' ? (
                    <Input
                      type="number"
                      value={curVal === undefined || curVal === null ? '' : String(curVal)}
                      onChange={(e) => handleSubFieldChange(subK, e.target.value)}
                      placeholder={sf?.placeholder || sf?.help_text?.slice(0, 30) || `请输入数值`}
                      className="h-7 text-xs font-mono bg-slate-50/70 dark:bg-slate-900 border-slate-200 dark:border-slate-700 text-slate-800 dark:text-slate-100 focus-visible:ring-purple-500"
                    />
                  ) : (
                    <Input
                      value={
                        curVal !== undefined && curVal !== null
                          ? typeof curVal === 'object'
                            ? JSON.stringify(curVal)
                            : String(curVal)
                          : ''
                      }
                      onChange={(e) => handleSubFieldChange(subK, e.target.value)}
                      placeholder={sf?.placeholder || sf?.help_text?.slice(0, 30) || `请输入 ${subK}`}
                      className="h-7 text-xs font-mono bg-slate-50/70 dark:bg-slate-900 border-slate-200 dark:border-slate-700 text-slate-800 dark:text-slate-100 focus-visible:ring-purple-500"
                    />
                  )}
                  {sf?.help_text && (
                    <span className="text-[9px] text-slate-400 dark:text-slate-500 line-clamp-1" title={sf.help_text}>
                      {sf.help_text}
                    </span>
                  )}
                </div>
              )
            })}
          </div>
        )
      ) : (
        <div className="flex flex-col gap-1">
          <textarea
            value={rawText}
            onChange={(e) => handleRawChange(e.target.value)}
            rows={Math.min(6, Math.max(3, rawText.split('\n').length))}
            className="w-full text-xs font-mono p-2 border border-purple-300 dark:border-purple-700 rounded-md bg-white dark:bg-slate-900 text-slate-800 dark:text-slate-100 focus:outline-none focus:ring-1 focus:ring-purple-500"
            placeholder='{\n  "ImageLayer": 1\n}'
          />
          {error && <span className="text-[10px] text-rose-500 font-mono">{error}</span>}
        </div>
      )}
    </div>
  )
}

interface StructuredObjectFormProps {
  field: ApiField
  value: any
  placeholder?: string
  onChange: (val: any) => void
}

const StructuredObjectForm: React.FC<StructuredObjectFormProps> = ({
  field,
  value,
  placeholder,
  onChange,
}) => {
  const [mode, setMode] = useState<'form' | 'json'>('form')

  const objValue: Record<string, any> = useMemo(() => {
    if (typeof value === 'object' && value !== null && !Array.isArray(value)) {
      return value
    }
    if (typeof value === 'string' && value.trim()) {
      try {
        const p = JSON.parse(value.trim())
        if (typeof p === 'object' && p !== null && !Array.isArray(p)) return p
      } catch {}
    }
    return {}
  }, [value])

  const subFields = field.sub_fields || []
  const hasSubFields = subFields.length > 0

  const displayedKeys = useMemo(() => {
    const keys = new Set<string>()
    subFields.forEach((sf) => keys.add(sf.name))
    Object.keys(objValue).forEach((k) => keys.add(k))
    return Array.from(keys)
  }, [subFields, objValue])

  const subFieldMap = useMemo(() => {
    const map = new Map<string, ApiField>()
    subFields.forEach((sf) => map.set(sf.name, sf))
    return map
  }, [subFields])

  const handleSubFieldChange = (key: string, val: any) => {
    const sf = subFieldMap.get(key)
    const category = sf ? getFieldTypeCategory(sf.type, val) : getFieldTypeCategory(undefined, val)

    let finalVal: any = val
    if (category === 'number') {
      if (val === '') {
        finalVal = undefined
      } else {
        const num = Number(val)
        finalVal = isNaN(num) ? val : num
      }
    } else if (category === 'boolean') {
      finalVal = Boolean(val)
    }

    const next = { ...objValue }
    if (finalVal === undefined) {
      delete next[key]
    } else {
      next[key] = finalVal
    }
    onChange(next)
  }

  const [jsonText, setJsonText] = useState(() => {
    try {
      return JSON.stringify(objValue, null, 2)
    } catch {
      return '{}'
    }
  })
  const [jsonErr, setJsonErr] = useState<string | null>(null)

  useEffect(() => {
    if (mode === 'form') {
      try {
        setJsonText(JSON.stringify(objValue, null, 2))
        setJsonErr(null)
      } catch {}
    }
  }, [objValue, mode])

  const handleJsonChange = (text: string) => {
    setJsonText(text)
    try {
      const parsed = JSON.parse(text)
      if (typeof parsed === 'object' && parsed !== null && !Array.isArray(parsed)) {
        setJsonErr(null)
        onChange(parsed)
      } else {
        setJsonErr('必须是 JSON 对象格式 (以 { 开头，} 结尾)')
      }
    } catch (e: any) {
      setJsonErr(e.message)
    }
  }

  return (
    <div className="flex flex-col gap-2 rounded-xl border border-purple-200/80 dark:border-purple-900/60 bg-purple-50/25 dark:bg-purple-950/20 p-3 shadow-2xs">
      <div className="flex items-center justify-between pb-1.5 border-b border-purple-100 dark:border-purple-900/40">
        <div className="flex items-center gap-2">
          <span className="font-mono text-[10px] font-bold bg-purple-100 dark:bg-purple-900/60 text-purple-700 dark:text-purple-300 px-1.5 py-0.5 rounded flex items-center gap-1">
            &#123; &#125; {field.struct_name || 'Object'}
          </span>
          <span className="text-xs font-semibold text-slate-700 dark:text-slate-300">
            {field.label || field.name}
          </span>
          {hasSubFields && (
            <span className="text-[10px] text-slate-400 dark:text-slate-500 font-mono">
              ({subFields.length} 个官方参数)
            </span>
          )}
        </div>

        <div className="flex items-center gap-1 bg-slate-200/70 dark:bg-slate-800 p-0.5 rounded-lg text-[10px]">
          <button
            type="button"
            onClick={() => setMode('form')}
            className={`px-2 py-0.5 rounded font-medium transition-all cursor-pointer ${
              mode === 'form'
                ? 'bg-white dark:bg-slate-700 text-purple-700 dark:text-purple-300 shadow-2xs font-semibold'
                : 'text-slate-600 dark:text-slate-400 hover:text-slate-900 dark:hover:text-slate-200'
            }`}
          >
            可视化表单
          </button>
          <button
            type="button"
            onClick={() => setMode('json')}
            className={`px-2 py-0.5 rounded font-medium transition-all cursor-pointer ${
              mode === 'json'
                ? 'bg-white dark:bg-slate-700 text-purple-700 dark:text-purple-300 shadow-2xs font-semibold'
                : 'text-slate-600 dark:text-slate-400 hover:text-slate-900 dark:hover:text-slate-200'
            }`}
          >
            JSON 代码
          </button>
        </div>
      </div>

      {mode === 'form' ? (
        displayedKeys.length === 0 ? (
          <div className="py-3 text-center text-xs text-slate-400 dark:text-slate-500 bg-white dark:bg-slate-900 rounded-lg border border-dashed border-slate-200 dark:border-slate-800">
            对象暂无字段内容，切换至 JSON 代码视图可自定义写入
          </div>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3 pt-1">
            {displayedKeys.map((key) => {
              const sf = subFieldMap.get(key)
              const curVal = objValue[key]
              const category = sf ? getFieldTypeCategory(sf.type, curVal) : getFieldTypeCategory(undefined, curVal)
              const isRequired = sf?.required ?? false

              return (
                <div key={key} className="flex flex-col gap-1">
                  <div className="flex items-center justify-between">
                    <label className="text-[11px] font-medium text-slate-700 dark:text-slate-300 flex items-center gap-1">
                      <span>{sf?.label || key}</span>
                      {isRequired && <span className="text-rose-500 font-bold">*</span>}
                      {sf && sf.label !== key && (
                        <span className="text-[10px] text-slate-400 font-mono font-normal">
                          ({key})
                        </span>
                      )}
                    </label>
                    <span className="text-[9px] font-mono px-1 rounded bg-slate-100 dark:bg-slate-800 text-slate-500">
                      {category}
                    </span>
                  </div>

                  {category === 'boolean' ? (
                    <div className="flex items-center gap-1.5">
                      <button
                        type="button"
                        onClick={() => handleSubFieldChange(key, true)}
                        className={`px-2.5 py-1 text-xs rounded border transition-all cursor-pointer ${
                          curVal === true
                            ? 'bg-emerald-600 text-white border-emerald-600 font-semibold'
                            : 'bg-white dark:bg-slate-800 text-slate-600 dark:text-slate-300 border-slate-200 dark:border-slate-700 hover:bg-slate-50'
                        }`}
                      >
                        ✓ True
                      </button>
                      <button
                        type="button"
                        onClick={() => handleSubFieldChange(key, false)}
                        className={`px-2.5 py-1 text-xs rounded border transition-all cursor-pointer ${
                          curVal === false
                            ? 'bg-rose-600 text-white border-rose-600 font-semibold'
                            : 'bg-white dark:bg-slate-800 text-slate-600 dark:text-slate-300 border-slate-200 dark:border-slate-700 hover:bg-slate-50'
                        }`}
                      >
                        ✕ False
                      </button>
                    </div>
                  ) : category === 'number' ? (
                    <Input
                      type="number"
                      value={curVal === undefined || curVal === null ? '' : String(curVal)}
                      onChange={(e) => handleSubFieldChange(key, e.target.value)}
                      placeholder={sf?.placeholder || sf?.help_text?.slice(0, 40) || `请输入数值`}
                      className="h-8 text-xs font-mono bg-white dark:bg-slate-900 border-slate-200 dark:border-slate-700 text-slate-800 dark:text-slate-100 focus-visible:ring-purple-500"
                    />
                  ) : (
                    <Input
                      value={curVal === undefined || curVal === null ? '' : typeof curVal === 'object' ? JSON.stringify(curVal) : String(curVal)}
                      onChange={(e) => handleSubFieldChange(key, e.target.value)}
                      placeholder={sf?.placeholder || sf?.help_text?.slice(0, 40) || `请输入 ${key}`}
                      className="h-8 text-xs font-mono bg-white dark:bg-slate-900 border-slate-200 dark:border-slate-700 text-slate-800 dark:text-slate-100 focus-visible:ring-purple-500"
                    />
                  )}
                  {sf?.help_text && (
                    <span className="text-[10px] text-slate-400 dark:text-slate-500 line-clamp-1" title={sf.help_text}>
                      {sf.help_text}
                    </span>
                  )}
                </div>
              )
            })}
          </div>
        )
      ) : (
        <div className="w-full">
          <JsonCodeEditor
            value={jsonText}
            onChange={handleJsonChange}
            error={jsonErr}
            height="180px"
            minHeight="120px"
            showLineNumbers={true}
            label={`${field.label || field.name} JSON`}
            hideStats={true}
          />
        </div>
      )}
    </div>
  )
}

interface DynamicListFormProps {
  value: any
  placeholder?: string
  field: ApiField
  onChange: (val: any[]) => void
}

const DynamicListForm: React.FC<DynamicListFormProps> = ({
  value,
  placeholder,
  field,
  onChange,
}) => {
  const [mode, setMode] = useState<'form' | 'json'>('form')
  const [showBatchModal, setShowBatchModal] = useState(false)
  const [batchInput, setBatchInput] = useState('')

  // 规范化当前数组
  const items: any[] = useMemo(() => {
    if (Array.isArray(value)) return value
    if (typeof value === 'string' && value.trim()) {
      const trimmed = value.trim()
      if (trimmed.startsWith('[') && trimmed.endsWith(']')) {
        try {
          const parsed = JSON.parse(trimmed)
          if (Array.isArray(parsed)) return parsed
        } catch {}
      }
      return trimmed
        .split(/[,，\n]/)
        .map((s) => s.trim())
        .filter(Boolean)
    }
    return []
  }, [value])

  // 是否为对象列表（如 CheckItem, ExtensionList, MemberList, InputStreamList 等）
  const isObjectList = useMemo(() => {
    if (items.some((x) => typeof x === 'object' && x !== null)) return true
    if (
      Array.isArray(field.default) &&
      field.default.some((x) => typeof x === 'object' && x !== null)
    )
      return true
    const fn = field.name
    if (
      fn.endsWith('List') ||
      fn.endsWith('Infos') ||
      fn.endsWith('Params') ||
      fn.endsWith('Items') ||
      fn === 'CheckItem' ||
      fn === 'DeleteItem' ||
      fn === 'ExtensionList' ||
      fn === 'RecentContactItem' ||
      fn === 'MemberList' ||
      fn === 'MsgSeqList' ||
      fn === 'FollowItem' ||
      fn === 'GroupContactItem' ||
      fn === 'AddFriendItem' ||
      fn === 'AuditItemList' ||
      fn === 'UserAttrs' ||
      fn === 'UserTags' ||
      fn === 'TopicPermissionList' ||
      fn === 'InputStreamList' ||
      fn === 'InputList' ||
      fn === 'AdaptiveChildren' ||
      fn === 'DomainInfos' ||
      fn === 'Images' ||
      fn === 'Keywords' ||
      fn === 'ImageInfos' ||
      fn === 'VideoInfos'
    ) {
      if (
        fn === 'GroupIdList' ||
        fn === 'To_Account' ||
        fn === 'Account' ||
        fn === 'StorageDays' ||
        fn === 'Labels' ||
        fn === 'TranscodeTemplateIds'
      ) {
        return false
      }
      return true
    }
    return false
  }, [items, field])

  // 是否为 MsgBody
  const isMsgBody = field.name === 'MsgBody'

  const itemSchemaFields = field.item_schema?.fields || []

  // 探测对象属性键名列表
  const objectKeys = useMemo(() => {
    const keysSet = new Set<string>()
    // 优先采用官方元数据定义的结构体字段
    itemSchemaFields.forEach((f) => keysSet.add(f.name))
    for (const it of items) {
      if (typeof it === 'object' && it !== null) {
        Object.keys(it).forEach((k) => keysSet.add(k))
      }
    }
    if (Array.isArray(field.default)) {
      for (const it of field.default) {
        if (typeof it === 'object' && it !== null) {
          Object.keys(it).forEach((k) => keysSet.add(k))
        }
      }
    }
    if (keysSet.size === 0) {
      if (field.name === 'CheckItem' || field.name === 'DeleteItem') keysSet.add('UserID')
      else if (field.name === 'ExtensionList') {
        keysSet.add('Key')
        keysSet.add('Value')
      } else if (field.name === 'MemberList') keysSet.add('Member_Account')
      else if (field.name === 'MsgSeqList') keysSet.add('MsgSeq')
      else if (field.name === 'FollowItem') keysSet.add('To_Account')
      else if (field.name === 'InputStreamList') {
        keysSet.add('InputStreamName')
        keysSet.add('LayoutParams')
      } else if (field.name === 'InputList') {
        keysSet.add('InputStreamName')
        keysSet.add('InputUrl')
      } else keysSet.add('Value')
    }
    return Array.from(keysSet)
  }, [items, field, itemSchemaFields])

  // 分析每个属性是否为嵌套结构（对象或数组）
  const objectPropertyTypes = useMemo(() => {
    const propTypes = new Map<string, { isObject: boolean; subKeys: string[] }>()
    for (const it of items) {
      if (typeof it === 'object' && it !== null) {
        for (const [k, v] of Object.entries(it)) {
          if (typeof v === 'object' && v !== null && !Array.isArray(v)) {
            const existing = propTypes.get(k) || { isObject: true, subKeys: [] }
            const subSet = new Set(existing.subKeys)
            Object.keys(v).forEach((sk) => subSet.add(sk))
            propTypes.set(k, { isObject: true, subKeys: Array.from(subSet) })
          } else if (!propTypes.has(k)) {
            propTypes.set(k, { isObject: false, subKeys: [] })
          }
        }
      }
    }
    return propTypes
  }, [items])

  // 基础列表操作
  const handleUpdatePrimitive = (index: number, newVal: string) => {
    const next = [...items]
    next[index] = newVal
    onChange(next)
  }

  const handleAddPrimitive = () => {
    onChange([...items, ''])
  }

  const handleRemovePrimitive = (index: number) => {
    const next = items.filter((_, i) => i !== index)
    onChange(next)
  }

  const handleApplyBatchInput = () => {
    const parsed = batchInput
      .split(/[,，\n]/)
      .map((s) => s.trim())
      .filter(Boolean)
    if (parsed.length > 0) {
      onChange([...items, ...parsed])
    }
    setBatchInput('')
    setShowBatchModal(false)
  }

  // 对象列表操作
  const handleUpdateObjectField = (index: number, key: string, val: any) => {
    const next = [...items]
    let finalVal = val
    if (typeof val === 'string') {
      const trimmed = val.trim()
      if (
        (trimmed.startsWith('{') && trimmed.endsWith('}')) ||
        (trimmed.startsWith('[') && trimmed.endsWith(']'))
      ) {
        try {
          finalVal = JSON.parse(trimmed)
        } catch {
          finalVal = val
        }
      }
    }
    next[index] = { ...(next[index] || {}), [key]: finalVal }
    onChange(next)
  }

  const handleAddObject = () => {
    const newObj: Record<string, any> = {}
    objectKeys.forEach((k) => {
      const schemaF = itemSchemaFields.find((f) => f.name === k)
      const propInfo = objectPropertyTypes.get(k)
      if (schemaF?.sub_fields && schemaF.sub_fields.length > 0) {
        const subObj: Record<string, any> = {}
        schemaF.sub_fields.forEach((sf) => {
          if (sf.name === 'ImageLayer') {
            subObj[sf.name] = items.length + 1
          } else if (sf.type === 'number') {
            subObj[sf.name] = sf.placeholder ? Number(sf.placeholder) || 0 : 0
          } else if (sf.type === 'boolean') {
            subObj[sf.name] = false
          } else {
            subObj[sf.name] = sf.placeholder || ''
          }
        })
        newObj[k] = subObj
      } else if (propInfo?.isObject) {
        const subObj: Record<string, any> = {}
        if (propInfo.subKeys.length > 0) {
          propInfo.subKeys.forEach((sk) => {
            const sampleVal = items[0]?.[k]?.[sk]
            subObj[sk] =
              typeof sampleVal === 'number'
                ? sk.includes('Layer')
                  ? items.length + 1
                  : 0
                : ''
          })
        }
        newObj[k] = subObj
      } else {
        newObj[k] = ''
      }
    })
    onChange([...items, newObj])
  }

  const handleRemoveObject = (index: number) => {
    const next = items.filter((_, i) => i !== index)
    onChange(next)
  }

  // MsgBody 专用操作
  const handleAddMsgElement = () => {
    const newElem = {
      MsgType: 'TIMTextElem',
      MsgContent: { Text: '' },
    }
    onChange([...items, newElem])
  }

  const handleUpdateMsgElementText = (index: number, text: string) => {
    const next = [...items]
    const cur = next[index] || { MsgType: 'TIMTextElem' }
    next[index] = {
      ...cur,
      MsgContent: { ...(cur.MsgContent || {}), Text: text },
    }
    onChange(next)
  }

  const handleUpdateMsgElementType = (index: number, type: string) => {
    const next = [...items]
    const cur = next[index] || {}
    next[index] = {
      ...cur,
      MsgType: type,
      MsgContent: cur.MsgContent || (type === 'TIMTextElem' ? { Text: '' } : {}),
    }
    onChange(next)
  }

  // Raw JSON 文本处理
  const jsonTextValue = useMemo(() => {
    try {
      return JSON.stringify(items, null, 2)
    } catch {
      return '[]'
    }
  }, [items])

  const [rawJsonDraft, setRawJsonDraft] = useState<string>(jsonTextValue)
  const [rawJsonErr, setRawJsonErr] = useState<string | null>(null)

  useEffect(() => {
    setRawJsonDraft(jsonTextValue)
  }, [jsonTextValue])

  const handleRawJsonChange = (text: string) => {
    setRawJsonDraft(text)
    try {
      const parsed = JSON.parse(text)
      if (Array.isArray(parsed)) {
        setRawJsonErr(null)
        onChange(parsed)
      } else {
        setRawJsonErr('必须是 JSON 数组格式（以 [ 开头，] 结尾）')
      }
    } catch (e: any) {
      setRawJsonErr(e.message)
    }
  }

  return (
    <div className="flex flex-col gap-2 rounded-xl border border-slate-200 dark:border-slate-800 bg-slate-50/50 dark:bg-slate-900/60 p-2.5 shadow-2xs">
      {/* 顶栏控制栏 */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className="text-[11px] font-semibold text-slate-700 dark:text-slate-300">
            {isMsgBody
              ? '消息分片动态表单'
              : isObjectList
              ? '对象数组动态表单'
              : '列表动态项表单'}
          </span>
          <span className="px-1.5 py-0.2 rounded-full bg-blue-100 dark:bg-blue-950/60 text-blue-700 dark:text-blue-300 text-[10px] font-mono font-bold">
            {items.length} 项
          </span>
        </div>

        {/* 模式切换 */}
        <div className="flex items-center gap-1 bg-slate-200/70 dark:bg-slate-800 p-0.5 rounded-lg text-[10px]">
          <button
            type="button"
            onClick={() => setMode('form')}
            className={`px-2 py-0.5 rounded font-medium transition-all ${
              mode === 'form'
                ? 'bg-white dark:bg-slate-700 text-blue-600 dark:text-blue-400 shadow-2xs font-semibold'
                : 'text-slate-600 dark:text-slate-400 hover:text-slate-900 dark:hover:text-slate-200'
            }`}
          >
            动态表单
          </button>
          <button
            type="button"
            onClick={() => setMode('json')}
            className={`px-2 py-0.5 rounded font-medium transition-all ${
              mode === 'json'
                ? 'bg-white dark:bg-slate-700 text-blue-600 dark:text-blue-400 shadow-2xs font-semibold'
                : 'text-slate-600 dark:text-slate-400 hover:text-slate-900 dark:hover:text-slate-200'
            }`}
          >
            JSON代码
          </button>
        </div>
      </div>

      {mode === 'json' ? (
        <div className="w-full">
          <JsonCodeEditor
            value={rawJsonDraft}
            onChange={handleRawJsonChange}
            error={rawJsonErr}
            height="180px"
            minHeight="120px"
            showLineNumbers={true}
            label={`${field.label || field.name} 数组 JSON`}
            hideStats={true}
          />
        </div>
      ) : isMsgBody ? (
        /* MsgBody 动态表单 */
        <div className="flex flex-col gap-2">
          {items.length === 0 ? (
            <div className="py-3 text-center border border-dashed border-slate-200 dark:border-slate-800 rounded-lg bg-white/70 dark:bg-slate-850/60">
              <p className="text-xs text-slate-400 dark:text-slate-500 mb-2">暂无消息元素，点击下方添加</p>
              <Button
                size="sm"
                variant="outline"
                onClick={handleAddMsgElement}
                className="h-7 text-xs text-blue-600 dark:text-blue-400 border-blue-200 dark:border-blue-800/60"
              >
                <Plus className="w-3.5 h-3.5 mr-1" /> 添加消息元素
              </Button>
            </div>
          ) : (
            items.map((elem, idx) => (
              <div
                key={idx}
                className="rounded-lg border border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-850 p-2.5 shadow-2xs flex flex-col gap-2"
              >
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    <span className="text-[10px] font-mono font-bold bg-slate-100 dark:bg-slate-800 text-slate-700 dark:text-slate-300 px-1.5 py-0.5 rounded">
                      #{idx + 1}
                    </span>
                    <span className="text-xs font-semibold text-slate-700 dark:text-slate-200">
                      消息元素 ({elem.MsgType || 'TIMTextElem'})
                    </span>
                  </div>
                  <button
                    type="button"
                    onClick={() => handleRemovePrimitive(idx)}
                    className="p-1 text-slate-400 hover:text-rose-600 rounded transition-colors"
                    title="删除此分片"
                  >
                    <Trash2 className="w-3.5 h-3.5" />
                  </button>
                </div>

                <div className="grid grid-cols-1 md:grid-cols-3 gap-2">
                  <div className="flex flex-col gap-1">
                    <label className="text-[10px] font-medium text-slate-500 dark:text-slate-400">
                      元素类型 (MsgType)
                    </label>
                    <select
                      value={elem.MsgType || 'TIMTextElem'}
                      onChange={(e) => handleUpdateMsgElementType(idx, e.target.value)}
                      className="h-8 text-xs bg-slate-50 dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded px-2 text-slate-800 dark:text-slate-200 focus:outline-none focus:ring-1 focus:ring-orange-500 font-mono"
                    >
                      <option value="TIMTextElem">TIMTextElem (文本消息)</option>
                      <option value="TIMCustomElem">TIMCustomElem (自定义消息)</option>
                      <option value="TIMFaceElem">TIMFaceElem (表情消息)</option>
                      <option value="TIMLocationElem">TIMLocationElem (地理位置)</option>
                      <option value="TIMStreamElem">TIMStreamElem (流式分片)</option>
                    </select>
                  </div>

                  <div className="flex flex-col gap-1 md:col-span-2">
                    <label className="text-[10px] font-medium text-slate-500">
                      {elem.MsgType === 'TIMTextElem' ? '文本内容 (Text)' : '内容结构 (MsgContent)'}
                    </label>
                    {elem.MsgType === 'TIMTextElem' ? (
                      <Input
                        value={elem.MsgContent?.Text ?? ''}
                        onChange={(e) => handleUpdateMsgElementText(idx, e.target.value)}
                        placeholder="请输入消息文本内容"
                        className="h-8 text-xs bg-white dark:bg-slate-900 border-slate-200 dark:border-slate-700 text-slate-800 dark:text-slate-100 focus-visible:ring-orange-500"
                      />
                    ) : (
                      <Input
                        value={
                          typeof elem.MsgContent === 'object'
                            ? JSON.stringify(elem.MsgContent)
                            : String(elem.MsgContent || '')
                        }
                        onChange={(e) => {
                          try {
                            const parsed = JSON.parse(e.target.value)
                            const next = [...items]
                            next[idx] = { ...elem, MsgContent: parsed }
                            onChange(next)
                          } catch {
                            const next = [...items]
                            next[idx] = { ...elem, MsgContent: e.target.value }
                            onChange(next)
                          }
                        }}
                        placeholder='例如: {"Data": "custom_data", "Desc": "info"}'
                        className="h-8 text-xs font-mono bg-white dark:bg-slate-900 border-slate-200 dark:border-slate-700 text-slate-800 dark:text-slate-100 focus-visible:ring-orange-500"
                      />
                    )}
                  </div>
                </div>
              </div>
            ))
          )}

          <div className="flex items-center justify-between pt-1">
            <button
              type="button"
              onClick={handleAddMsgElement}
              className="inline-flex items-center gap-1.5 text-xs text-blue-600 dark:text-blue-400 hover:text-blue-700 dark:hover:text-blue-300 font-semibold px-2.5 py-1 rounded-md hover:bg-blue-50 dark:hover:bg-blue-950/40 border border-blue-200 dark:border-blue-800/80 border-dashed transition-all"
            >
              <Plus className="w-3.5 h-3.5" />
              添加消息元素分片
            </button>
            {items.length > 0 && (
              <button
                type="button"
                onClick={() => onChange([])}
                className="text-[11px] text-slate-400 dark:text-slate-500 hover:text-rose-500 transition-colors"
              >
                清空全部 ({items.length})
              </button>
            )}
          </div>
        </div>
      ) : isObjectList ? (
        /* 结构化对象动态表单 */
        <div className="flex flex-col gap-2">
          {items.length === 0 ? (
            <div className="py-3 text-center border border-dashed border-slate-200 dark:border-slate-800 rounded-lg bg-white/70 dark:bg-slate-850/60">
              <p className="text-xs text-slate-400 dark:text-slate-500 mb-2">当前列表为空</p>
              <Button
                size="sm"
                variant="outline"
                onClick={handleAddObject}
                className="h-7 text-xs text-blue-600 dark:text-blue-400 border-blue-200 dark:border-blue-800/80"
              >
                <Plus className="w-3.5 h-3.5 mr-1" /> 添加第一条数据
              </Button>
            </div>
          ) : (
            items.map((item, idx) => {
              // 聚合当前元素的所有属性，分离基础类型属性与嵌套复杂对象
              const currentItemKeys = Array.from(
                new Set([...objectKeys, ...Object.keys(item || {})])
              )
              const primitiveKeys = currentItemKeys.filter(
                (k) =>
                  !objectPropertyTypes.get(k)?.isObject &&
                  (typeof item[k] !== 'object' || item[k] === null || Array.isArray(item[k]))
              )
              const nestedObjectKeys = currentItemKeys.filter(
                (k) =>
                  objectPropertyTypes.get(k)?.isObject ||
                  (typeof item[k] === 'object' && item[k] !== null && !Array.isArray(item[k]))
              )

              return (
                <div
                  key={idx}
                  className="rounded-lg border border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-850 p-2.5 shadow-2xs flex flex-col gap-2.5"
                >
                  <div className="flex items-center justify-between">
                    <div className="flex items-center gap-2">
                      <span className="text-[10px] font-mono font-bold bg-slate-100 dark:bg-slate-800 text-slate-700 dark:text-slate-300 px-1.5 py-0.5 rounded">
                        #{idx + 1}
                      </span>
                      {field.name === 'InputStreamList' && item.InputStreamName && (
                        <span className="text-[11px] font-mono text-slate-500 dark:text-slate-400 font-medium">
                          {item.InputStreamName}
                        </span>
                      )}
                    </div>
                    <button
                      type="button"
                      onClick={() => handleRemoveObject(idx)}
                      className="p-1 text-slate-400 hover:text-rose-600 rounded transition-colors cursor-pointer"
                      title="删除此项"
                    >
                      <Trash2 className="w-3.5 h-3.5" />
                    </button>
                  </div>

                  {/* 基础字段网格 */}
                  {primitiveKeys.length > 0 && (
                    <div
                      className={`grid gap-2 ${
                        primitiveKeys.length > 1 ? 'grid-cols-1 md:grid-cols-2' : 'grid-cols-1'
                      }`}
                    >
                      {primitiveKeys.map((k) => {
                        const schemaF = itemSchemaFields.find((f) => f.name === k)
                        return (
                          <div key={k} className="flex flex-col gap-1">
                            <label className="text-[10px] font-medium text-slate-500 dark:text-slate-400 font-mono flex items-center gap-1">
                              <span>{schemaF?.label || k}</span>
                              {schemaF?.required && <span className="text-rose-500 font-bold">*</span>}
                              {schemaF && schemaF.label !== k && (
                                <span className="text-slate-400 font-normal">({k})</span>
                              )}
                            </label>
                            <Input
                              value={
                                item[k] !== undefined && item[k] !== null
                                  ? Array.isArray(item[k])
                                    ? JSON.stringify(item[k])
                                    : String(item[k])
                                  : ''
                              }
                              onChange={(e) => handleUpdateObjectField(idx, k, e.target.value)}
                              placeholder={schemaF?.placeholder || schemaF?.help_text?.slice(0, 30) || `请输入 ${k}`}
                              className="h-7 text-xs font-mono bg-slate-50/60 dark:bg-slate-900 border-slate-200 dark:border-slate-700 text-slate-800 dark:text-slate-100 focus-visible:ring-blue-500"
                            />
                            {schemaF?.help_text && (
                              <span className="text-[9px] text-slate-400 dark:text-slate-500 line-clamp-1" title={schemaF.help_text}>
                                {schemaF.help_text}
                              </span>
                            )}
                          </div>
                        )
                      })}
                    </div>
                  )}

                  {/* 嵌套复杂对象 (如 LayoutParams, CropParams 等) */}
                  {nestedObjectKeys.map((k) => {
                    const schemaF = itemSchemaFields.find((f) => f.name === k)
                    return (
                      <NestedObjectEditor
                        key={k}
                        parentKey={k}
                        structName={schemaF?.struct_name}
                        subFieldsDef={schemaF?.sub_fields}
                        value={item[k]}
                        onChange={(val) => handleUpdateObjectField(idx, k, val)}
                      />
                    )
                  })}
                </div>
              )
            })
          )}

          <div className="flex items-center justify-between pt-1">
            <button
              type="button"
              onClick={handleAddObject}
              className="inline-flex items-center gap-1.5 text-xs text-blue-600 dark:text-blue-400 hover:text-blue-700 dark:hover:text-blue-300 font-semibold px-2.5 py-1 rounded-md hover:bg-blue-50 dark:hover:bg-blue-950/40 border border-blue-200 dark:border-blue-800/80 border-dashed transition-all"
            >
              <Plus className="w-3.5 h-3.5" />
              添加一项数据
            </button>
            {items.length > 0 && (
              <button
                type="button"
                onClick={() => onChange([])}
                className="text-[11px] text-slate-400 dark:text-slate-500 hover:text-rose-500 transition-colors"
              >
                清空全部 ({items.length})
              </button>
            )}
          </div>
        </div>
      ) : (
        /* 一维简单列表动态表单（如 To_Account, GroupIdList, Member_Accounts 等） */
        <div className="flex flex-col gap-2">
          {items.length === 0 ? (
            <div className="py-3 text-center border border-dashed border-slate-200 dark:border-slate-800 rounded-lg bg-white/70 dark:bg-slate-850/60 flex flex-col items-center gap-2">
              <span className="text-xs text-slate-400 dark:text-slate-500">列表暂无内容</span>
              <div className="flex items-center gap-2">
                <Button
                  size="sm"
                  variant="outline"
                  onClick={handleAddPrimitive}
                  className="h-7 text-xs border-blue-200 dark:border-blue-800 text-blue-600 dark:text-blue-400 hover:bg-blue-50 dark:hover:bg-blue-950/40"
                >
                  <Plus className="w-3.5 h-3.5 mr-1" />
                  添加第一项
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => setShowBatchModal(true)}
                  className="h-7 text-xs text-blue-600 dark:text-blue-400 hover:bg-blue-50 dark:hover:bg-blue-950/40 font-medium"
                >
                  ⚡ 批量快捷导入
                </Button>
              </div>
            </div>
          ) : (
            <div className="flex flex-col gap-1.5 max-h-72 overflow-y-auto pr-1">
              {items.map((item, idx) => (
                <div key={idx} className="flex items-center gap-2 group">
                  <span className="text-[10px] text-slate-400 dark:text-slate-500 font-mono w-5 text-right select-none">
                    #{idx + 1}
                  </span>
                  <Input
                    value={item}
                    onChange={(e) => handleUpdatePrimitive(idx, e.target.value)}
                    placeholder={placeholder || `第 ${idx + 1} 项`}
                    className="h-8 text-xs bg-white dark:bg-slate-900 border-slate-200 dark:border-slate-700 text-slate-800 dark:text-slate-100 focus-visible:ring-orange-500 font-mono flex-1"
                  />
                  <button
                    type="button"
                    onClick={() => handleRemovePrimitive(idx)}
                    className="p-1.5 text-slate-300 dark:text-slate-600 group-hover:text-slate-400 hover:!text-rose-600 hover:bg-rose-50 dark:hover:bg-rose-950/40 rounded transition-colors"
                    title="删除此行"
                  >
                    <Trash2 className="w-3.5 h-3.5" />
                  </button>
                </div>
              ))}
            </div>
          )}

          {/* 批量快速导入折叠面板 */}
          {showBatchModal && (
            <div className="p-2.5 bg-white dark:bg-slate-850 border border-blue-200 dark:border-blue-900/60 rounded-lg flex flex-col gap-2 shadow-2xs animate-in fade-in duration-150">
              <div className="flex items-center justify-between text-[11px]">
                <span className="font-semibold text-slate-700 dark:text-slate-300">⚡ 批量快捷输入 / 粘贴</span>
                <span className="text-slate-400 dark:text-slate-500 text-[10px]">
                  支持英文逗号、中文逗号或每行一项
                </span>
              </div>
              <textarea
                rows={3}
                value={batchInput}
                onChange={(e) => setBatchInput(e.target.value)}
                placeholder="例如:&#10;user1, user2&#10;user3&#10;user4"
                className="w-full text-xs font-mono p-2 border border-slate-300 dark:border-slate-700 rounded bg-slate-50 dark:bg-slate-900 text-slate-800 dark:text-slate-100 focus:bg-white dark:focus:bg-slate-900 focus:outline-none focus:ring-1 focus:ring-orange-500"
              />
              <div className="flex justify-end gap-2">
                <Button
                  size="sm"
                  variant="ghost"
                  className="h-6 text-[11px] text-slate-500 dark:text-slate-400"
                  onClick={() => setShowBatchModal(false)}
                >
                  取消
                </Button>
                <Button
                  size="sm"
                  className="h-6 text-[11px] bg-blue-600 hover:bg-blue-500 text-white font-medium cursor-pointer"
                  onClick={handleApplyBatchInput}
                >
                  导入并解析为表单项
                </Button>
              </div>
            </div>
          )}

          {/* 底部操作工具栏 */}
          <div className="flex items-center justify-between pt-0.5">
            <button
              type="button"
              onClick={handleAddPrimitive}
              className="inline-flex items-center gap-1 text-xs text-blue-600 dark:text-blue-400 hover:text-blue-700 dark:hover:text-blue-300 font-semibold px-2.5 py-1 rounded-md hover:bg-blue-50 dark:hover:bg-blue-950/40 border border-blue-200 dark:border-blue-800/80 border-dashed transition-all"
            >
              <Plus className="w-3.5 h-3.5" />
              添加一行
            </button>

            <div className="flex items-center gap-2.5">
              <button
                type="button"
                onClick={() => setShowBatchModal(!showBatchModal)}
                className="text-[11px] text-blue-600 hover:text-blue-700 hover:underline flex items-center gap-1 font-medium"
              >
                {showBatchModal ? '收起批量导入' : '⚡ 批量快捷导入'}
              </button>
              {items.length > 0 && (
                <button
                  type="button"
                  onClick={() => onChange([])}
                  className="text-[11px] text-slate-400 hover:text-rose-500 transition-colors"
                >
                  清空全部 ({items.length})
                </button>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

interface JsonObjectInputProps {
  value: any
  onChange: (val: any) => void
  placeholder?: string
}

const JsonObjectInput: React.FC<JsonObjectInputProps> = ({
  value,
  onChange,
  placeholder,
}) => {
  const formatToJson = (v: any) => {
    if (typeof v === 'string') return v
    try {
      return JSON.stringify(v || {}, null, 2)
    } catch {
      return '{}'
    }
  }

  const [text, setText] = useState<string>(() => formatToJson(value))
  const [error, setError] = useState<string | null>(null)
  const [isFocused, setIsFocused] = useState(false)

  useEffect(() => {
    if (!isFocused) {
      setText(formatToJson(value))
    }
  }, [value, isFocused])

  const handleChange = (newText: string) => {
    setText(newText)
    try {
      const parsed = JSON.parse(newText)
      setError(null)
      onChange(parsed)
    } catch (e: any) {
      setError(e.message)
    }
  }

  return (
    <div onFocus={() => setIsFocused(true)} onBlur={() => setIsFocused(false)} className="w-full">
      <JsonCodeEditor
        value={text}
        onChange={handleChange}
        error={error}
        height="140px"
        minHeight="100px"
        placeholder={placeholder || '{\n  "key": "value"\n}'}
        showLineNumbers={true}
        label="JSON 对象"
        hideStats={true}
      />
    </div>
  )
}

interface ApiWorkbenchPanelProps {
  onSwitchToBatch?: () => void
}

export const ApiWorkbenchPanel: React.FC<ApiWorkbenchPanelProps> = ({
  onSwitchToBatch,
}) => {
  const {
    apis,
    activeApi,
    tabs,
    activeTabId,
    openTab,
    closeTab,
    closeOtherTabs,
    closeTabsToRight,
    closeAllTabs,
    setActiveTabId,
    updateTab,
    showToast,
    isInvoking,
    invokeResponse,
    invokeSingleApi,
    isPreviewMode,
    setPreviewMode,
    togglePreviewMode,
    clearTabInvokeHistory,
    restoreTabInvokeHistory,
    getRecentApis,
    getFrequentApis,
  } = useApiStore()
  const { setApiId } = useBatchStore()
  const { config } = useConfigStore()

  const currentTab = useMemo(() => {
    return tabs.find((t) => t.id === activeTabId) || null
  }, [tabs, activeTabId])

  // Response 调用历史下拉状态 (Postman风格)
  const [isResponseHistoryOpen, setIsResponseHistoryOpen] = useState<boolean>(false)
  const responseHistoryDropdownRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const handleClickOutside = (e: MouseEvent) => {
      if (
        responseHistoryDropdownRef.current &&
        !responseHistoryDropdownRef.current.contains(e.target as Node)
      ) {
        setIsResponseHistoryOpen(false)
      }
    }
    if (isResponseHistoryOpen) {
      document.addEventListener('mousedown', handleClickOutside)
    }
    return () => document.removeEventListener('mousedown', handleClickOutside)
  }, [isResponseHistoryOpen])

  // Tab 下拉搜索与右键菜单状态
  const [showTabDropdown, setShowTabDropdown] = useState<boolean>(false)
  const [tabSearchQuery, setTabSearchQuery] = useState<string>('')
  const dropdownRef = useRef<HTMLDivElement>(null)
  const searchInputRef = useRef<HTMLInputElement>(null)
  const tabsContainerRef = useRef<HTMLDivElement>(null)
  const activeTabRef = useRef<HTMLDivElement>(null)

  // Response 分隔面板拖拽与双击展开/还原控制
  const workbenchGroupRef = useGroupRef()
  const prevResponseHeightRef = useRef<number>(48)
  const isResponseMaximizedRef = useRef<boolean>(false)

  const handleToggleResponseHeight = () => {
    if (!workbenchGroupRef.current) return
    try {
      const layout = workbenchGroupRef.current.getLayout()
      const respSize = layout['response-panel'] ?? 48
      if (respSize >= 70) {
        // 当前为最大化状态：恢复到先前记录的高度或默认 48%
        const restoreSize = (prevResponseHeightRef.current && prevResponseHeightRef.current < 70)
          ? prevResponseHeightRef.current
          : 48
        workbenchGroupRef.current.setLayout({
          'request-panel': 100 - restoreSize,
          'response-panel': restoreSize,
        })
        isResponseMaximizedRef.current = false
      } else {
        // 放大展开 Response 窗格至 80%
        prevResponseHeightRef.current = respSize
        workbenchGroupRef.current.setLayout({
          'request-panel': 20,
          'response-panel': 80,
        })
        isResponseMaximizedRef.current = true
      }
    } catch (e) {
      console.warn('Toggle response height error:', e)
    }
  }

  // 当 activeTabId 变化（打开新标签或切换已有标签）时，自动平滑滚动定位到该标签
  useEffect(() => {
    if (activeTabId && activeTabRef.current) {
      requestAnimationFrame(() => {
        activeTabRef.current?.scrollIntoView({
          behavior: 'smooth',
          block: 'nearest',
          inline: 'nearest',
        })
      })
    }
  }, [activeTabId, tabs.length])

  interface ContextMenuState {
    visible: boolean
    x: number
    y: number
    tab: ApiTab | null
  }
  const [contextMenu, setContextMenu] = useState<ContextMenuState>({
    visible: false,
    x: 0,
    y: 0,
    tab: null,
  })

  // 过滤已打开的 Tab 和其它未打开的 API
  const filteredOpenTabs = useMemo(() => {
    if (!tabSearchQuery.trim()) return tabs
    return tabs.filter((t) => smartMatchApi(t.api, tabSearchQuery))
  }, [tabs, tabSearchQuery])

  const availableApis = useMemo(() => {
    const openIds = new Set(tabs.map((t) => t.apiId))
    const list = apis.filter((a) => !openIds.has(a.api_id))
    if (!tabSearchQuery.trim()) return list
    return list.filter((a) => smartMatchApi(a, tabSearchQuery))
  }, [apis, tabs, tabSearchQuery])

  // 右键菜单打开处理器
  const handleTabContextMenu = (e: React.MouseEvent, tab: ApiTab) => {
    e.preventDefault()
    e.stopPropagation()
    const menuWidth = 190
    const menuHeight = 240
    const x = e.clientX + menuWidth > window.innerWidth ? window.innerWidth - menuWidth - 8 : e.clientX
    const y = e.clientY + menuHeight > window.innerHeight ? window.innerHeight - menuHeight - 8 : e.clientY
    setContextMenu({
      visible: true,
      x,
      y,
      tab,
    })
  }

  // 全局点击自动关闭右键菜单和下拉弹窗
  useEffect(() => {
    const handleGlobalClick = (e: MouseEvent) => {
      if (contextMenu.visible) {
        setContextMenu((prev) => ({ ...prev, visible: false }))
      }
      if (
        showTabDropdown &&
        dropdownRef.current &&
        !dropdownRef.current.contains(e.target as Node)
      ) {
        setShowTabDropdown(false)
      }
    }
    window.addEventListener('mousedown', handleGlobalClick)
    return () => window.removeEventListener('mousedown', handleGlobalClick)
  }, [contextMenu.visible, showTabDropdown])

  // Postman/Apifox 请求与响应 Tab 状态
  const [requestTab, setRequestTab] = useState<'params' | 'body' | 'headers' | 'docs'>('body')
  const [bodyMode, setBodyMode] = useState<'form' | 'json'>('form')
  const [responseTab, setResponseTab] = useState<'pretty' | 'sent' | 'curl' | 'raw'>('pretty')

  const [formData, setFormData] = useState<Record<string, any>>({})
  const [jsonText, setJsonText] = useState<string>('{}')
  const [jsonError, setJsonError] = useState<string | null>(null)
  const [copied, setCopied] = useState<boolean>(false)
  const [copiedCurl, setCopiedCurl] = useState<boolean>(false)
  const [isResponseCollapsed, setIsResponseCollapsed] = useState<boolean>(false)
  const [isDocsModalOpen, setIsDocsModalOpen] = useState<boolean>(false)

  // 动态融合预定义字段与当前 payload 中的所有字段，确保所有参数在表单模式下均清晰可视并可编辑
  const effectiveFields = useMemo(() => {
    const predefined = activeApi?.fields || []
    const predefinedNames = new Set(predefined.map((f) => f.name))
    const extraFields: ApiField[] = []

    if (formData && typeof formData === 'object' && !Array.isArray(formData)) {
      Object.entries(formData).forEach(([key, val]) => {
        if (!predefinedNames.has(key)) {
          let inferredType = 'string'
          if (typeof val === 'boolean') {
            inferredType = 'boolean'
          } else if (typeof val === 'number') {
            inferredType = 'number'
          } else if (Array.isArray(val)) {
            inferredType = 'list'
          } else if (typeof val === 'object' && val !== null) {
            inferredType = 'json'
          }
          extraFields.push({
            name: key,
            label: key,
            type: inferredType,
            required: false,
            default: val,
            placeholder: `参数 ${key}`,
            help_text: '从官方示例或外部载入报文中自动提取的参数',
            aliases: [],
          })
        }
      })
    }

    const normalizedPredefined = predefined.map((f) => {
      const currentVal = formData?.[f.name] ?? f.default
      if (currentVal !== undefined && currentVal !== null) {
        if (typeof currentVal === 'object') {
          if (Array.isArray(currentVal) && f.type !== 'list') {
            return { ...f, type: 'list' }
          } else if (!Array.isArray(currentVal) && f.type !== 'json') {
            return { ...f, type: 'json' }
          }
        }
      }
      return f
    })

    return [...normalizedPredefined, ...extraFields]
  }, [activeApi?.fields, formData])

  // ESC 快捷键关闭文档放大弹窗
  useEffect(() => {
    const handleEsc = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && isDocsModalOpen) {
        setIsDocsModalOpen(false)
      }
    }
    window.addEventListener('keydown', handleEsc)
    return () => window.removeEventListener('keydown', handleEsc)
  }, [isDocsModalOpen])

  // 快捷键 ⌘+Enter / Ctrl+Enter 发送请求或执行预览
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
        e.preventDefault()
        handleSend()
      }
    }
    window.addEventListener('keydown', handleKeyDown)
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [activeApi, formData, jsonText, bodyMode, isInvoking, jsonError, isPreviewMode, effectiveFields])

  // 当切换 Tab 或外部载入数据（如从文档中心应用示例）时，恢复表单数据、JSON 文本与子 Tab 状态
  useEffect(() => {
    if (currentTab) {
      setFormData(currentTab.formData || {})
      setJsonText(currentTab.jsonText || '{}')
      setJsonError(currentTab.jsonError || null)
      if (currentTab.requestTab) setRequestTab(currentTab.requestTab)
      if (currentTab.bodyMode) setBodyMode(currentTab.bodyMode)
      if (currentTab.responseTab) setResponseTab(currentTab.responseTab)
    }
  }, [currentTab?.id, currentTab?.dataVersion])

  const handleFieldChange = (fieldName: string, value: any) => {
    const next = { ...formData, [fieldName]: value }
    const nextJson = JSON.stringify(next, null, 2)
    setFormData(next)
    setJsonText(nextJson)
    setJsonError(null)
    if (currentTab) {
      updateTab(currentTab.id, {
        formData: next,
        jsonText: nextJson,
        jsonError: null,
        isModified: true,
      })
    }
  }

  const handleJsonChange = (text: string) => {
    setJsonText(text)
    let parsed: any = null
    let err: string | null = null
    try {
      parsed = JSON.parse(text)
      const normalizedForm = { ...(typeof parsed === 'object' && parsed !== null ? parsed : {}) }
      if (!normalizedForm.Text && parsed?.MsgBody?.[0]?.MsgContent?.Text) {
        normalizedForm.Text = parsed.MsgBody[0].MsgContent.Text
      }
      setFormData(normalizedForm)
      setJsonError(null)
    } catch (e: any) {
      err = e.message
      setJsonError(e.message)
    }
    if (currentTab) {
      updateTab(currentTab.id, {
        formData: parsed || formData,
        jsonText: text,
        jsonError: err,
        isModified: true,
      })
    }
  }

  const handlePrettifyJson = () => {
    try {
      const parsed = JSON.parse(jsonText)
      const prettified = JSON.stringify(parsed, null, 2)
      setJsonText(prettified)
      setJsonError(null)
      if (currentTab) {
        updateTab(currentTab.id, { jsonText: prettified, jsonError: null })
      }
    } catch (e: any) {
      setJsonError(e.message)
    }
  }

  const handleReset = () => {
    if (activeApi && currentTab) {
      const initialData = initializeFormData(activeApi)
      const text = JSON.stringify(initialData, null, 2)
      setFormData(initialData)
      setJsonText(text)
      setJsonError(null)
      updateTab(currentTab.id, {
        formData: initialData,
        jsonText: text,
        jsonError: null,
        isModified: false,
      })
    }
  }

  const handleSetRequestTab = (tab: 'params' | 'body' | 'headers' | 'docs') => {
    setRequestTab(tab)
    if (currentTab) {
      updateTab(currentTab.id, { requestTab: tab })
    }
  }

  const handleApplyDocExample = (example: any) => {
    const raw = example.clean_json || example.content
    try {
      const parsed = JSON.parse(raw)
      const normalizedForm = { ...parsed }
      if (!normalizedForm.Text && parsed.MsgBody?.[0]?.MsgContent?.Text) {
        normalizedForm.Text = parsed.MsgBody[0].MsgContent.Text
      }
      if (!normalizedForm.Tags && parsed.UserTags?.[0]?.Tags) {
        normalizedForm.Tags = parsed.UserTags[0].Tags
      }
      const prettyJson = JSON.stringify(parsed, null, 2)
      setFormData(normalizedForm)
      setJsonText(prettyJson)
      setJsonError(null)
      setRequestTab('body')
      setBodyMode('form')
      if (currentTab) {
        updateTab(currentTab.id, {
          formData: normalizedForm,
          jsonText: prettyJson,
          jsonError: null,
          isModified: true,
          requestTab: 'body',
          bodyMode: 'form',
          dataVersion: Date.now(),
        })
      }
      showToast({
        type: 'success',
        message: `已载入示例「${example.title}」至请求体，表单与 JSON 均已同步`,
      })
    } catch {
      setJsonText(raw)
      setRequestTab('body')
      setBodyMode('json')
      if (currentTab) {
        updateTab(currentTab.id, {
          jsonText: raw,
          isModified: true,
          requestTab: 'body',
          bodyMode: 'json',
          dataVersion: Date.now(),
        })
      }
    }
  }

  const handleSetBodyMode = (mode: 'form' | 'json') => {
    if (mode === 'json' && bodyMode === 'form') {
      const sanitized = sanitizePayloadTypes(formData, effectiveFields)
      const prettyJson = JSON.stringify(sanitized, null, 2)
      setFormData(sanitized)
      setJsonText(prettyJson)
      setJsonError(null)
      if (currentTab) {
        updateTab(currentTab.id, {
          formData: sanitized,
          jsonText: prettyJson,
          jsonError: null,
          bodyMode: mode,
        })
      }
    } else if (mode === 'form' && bodyMode === 'json') {
      try {
        const parsed = JSON.parse(jsonText)
        const normalizedForm = { ...parsed }
        if (!normalizedForm.Text && parsed.MsgBody?.[0]?.MsgContent?.Text) {
          normalizedForm.Text = parsed.MsgBody[0].MsgContent.Text
        }
        setFormData(normalizedForm)
        setJsonError(null)
        if (currentTab) {
          updateTab(currentTab.id, {
            formData: normalizedForm,
            jsonError: null,
            bodyMode: mode,
          })
        }
      } catch (e: any) {
        setJsonError('无法切换到表单模式: 当前 JSON 存在语法错误 (' + e.message + ')')
        return
      }
    }
    setBodyMode(mode)
    if (currentTab) {
      updateTab(currentTab.id, { bodyMode: mode })
    }
  }

  const handleSetResponseTab = (tab: 'pretty' | 'sent' | 'curl' | 'raw') => {
    setResponseTab(tab)
    if (currentTab) {
      updateTab(currentTab.id, { responseTab: tab })
    }
  }

  const handleSend = async () => {
    if (!activeApi || isInvoking) return
    if (isResponseCollapsed) {
      setIsResponseCollapsed(false)
    }
    let payload = formData
    if (bodyMode === 'json') {
      try {
        payload = JSON.parse(jsonText)
      } catch (e: any) {
        setJsonError('JSON 语法错误: ' + e.message)
        handleSetRequestTab('body')
        return
      }
    } else {
      payload = sanitizePayloadTypes(formData, effectiveFields)
    }
    const res = await invokeSingleApi(activeApi.api_id, payload, isPreviewMode)
    if (currentTab) {
      updateTab(currentTab.id, { invokeResponse: res })
    }
  }

  const handleCopy = (content: string) => {
    navigator.clipboard.writeText(content)
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }

  const handleSetBatch = () => {
    if (activeApi) {
      setApiId(activeApi.api_id)
      if (onSwitchToBatch) onSwitchToBatch()
    }
  }

  const isApi3 = Boolean(activeApi?.service_type && activeApi.service_type !== 'im')
  const endpointHost = isApi3
    ? (config.tc_endpoint || activeApi?.endpoint || `${activeApi?.service_name || 'live'}.tencentcloudapi.com`)
    : (config.domain || 'console.tim.qq.com')

  // 拼接完整的 URL
  const fullCommand = useMemo(() => {
    if (!activeApi) return ''
    if (isApi3) {
      return activeApi.command
    }
    if (activeApi.command?.startsWith('v4/')) return activeApi.command
    const svc = activeApi.service_name ? `${activeApi.service_name}/` : ''
    return `v4/${svc}${activeApi.command}`
  }, [activeApi, isApi3])

  const fullUrl = useMemo(() => {
    if (!activeApi) return ''
    if (isApi3) {
      // 腾讯云 API 3.0 POST 规范中，URL 路径统一为根路径 /，Action 通过 Header (X-TC-Action) 传递
      return `https://${endpointHost}/`
    }
    return `https://${endpointHost}/${fullCommand}`
  }, [activeApi, isApi3, endpointHost, fullCommand])

  // 生成标准的 cURL 调用命令
  const curlCommand = useMemo(() => {
    if (!activeApi) return ''
    const currentPayload =
      bodyMode === 'json'
        ? jsonText
        : JSON.stringify(sanitizePayloadTypes(formData, activeApi?.fields), null, 2)

    if (isApi3) {
      const endpoint = config.tc_endpoint || activeApi.endpoint || `${activeApi.service_name || 'live'}.tencentcloudapi.com`
      const action = activeApi.action || activeApi.command
      const version = activeApi.version || '2018-08-01'
      const region = config.tc_region || 'ap-guangzhou'
      const secId = config.tc_secret_id || 'YOUR_SECRET_ID'
      return `curl -X POST "https://${endpoint}" \\\n  -H "Content-Type: application/json; charset=utf-8" \\\n  -H "X-TC-Action: ${action}" \\\n  -H "X-TC-Version: ${version}" \\\n  -H "X-TC-Region: ${region}" \\\n  -H "X-TC-Timestamp: $(date +%s)" \\\n  -H "Authorization: TC3-HMAC-SHA256 Credential=${secId}/..." \\\n  -d '${currentPayload}'`
    }

    const sdkappid = config.sdkappid || 1400000000
    const admin = config.admin_user || 'administrator'
    const queryUrl = `${fullUrl}?sdkappid=${sdkappid}&identifier=${admin}&usersig=USER_SIG_HERE&random=99999999&contenttype=json`
    return `curl -X POST "${queryUrl}" \\\n  -H "Content-Type: application/json;charset=utf-8" \\\n  -d '${currentPayload}'`
  }, [fullUrl, activeApi, isApi3, bodyMode, jsonText, formData, config])

  const [showPlainCurl, setShowPlainCurl] = useState<boolean>(false)

  const effectiveCurlCommand = useMemo(() => {
    return invokeResponse?.curl_command || curlCommand
  }, [invokeResponse?.curl_command, curlCommand])

  // 判断当前 cURL 命令是否包含已生成的真实有效签名凭据（UserSig 或 TC3 Authorization）
  const hasRealCredentials = useMemo(() => {
    if (!effectiveCurlCommand) return false
    const usersigMatch = effectiveCurlCommand.match(/[?&]usersig=([^&"'\s\\]+)/)
    if (
      usersigMatch &&
      usersigMatch[1] !== 'USER_SIG_HERE' &&
      usersigMatch[1] !== '<redacted>' &&
      !usersigMatch[1].includes('***')
    ) {
      return true
    }
    const tc3Match = effectiveCurlCommand.match(/Authorization:\s*TC3-HMAC-SHA256\s+Credential=([^/\s"']+)/i)
    if (
      tc3Match &&
      tc3Match[1] !== 'YOUR_SECRET_ID' &&
      tc3Match[1] !== '<redacted>' &&
      !effectiveCurlCommand.includes('Credential=YOUR_SECRET_ID') &&
      !effectiveCurlCommand.includes('<redacted>') &&
      !effectiveCurlCommand.includes('Signature=***')
    ) {
      return true
    }
    return false
  }, [effectiveCurlCommand])

  // 脱敏后的 cURL 命令（用于屏幕默认安全展示与安全分享模板）
  const maskedCurlCommand = useMemo(() => {
    if (!effectiveCurlCommand) return ''
    let masked = effectiveCurlCommand
    // 脱敏 IM UserSig: 保留前 10 位与后 6 位，中间使用国际通用的星号掩码
    masked = masked.replace(/([?&]usersig=)([^&"'\s\\]+)/g, (fullMatch, prefix, sig) => {
      if (sig === 'USER_SIG_HERE' || sig === '<redacted>') return fullMatch
      if (sig.length > 24) {
        return `${prefix}${sig.slice(0, 10)}...******...${sig.slice(-6)}`
      }
      return `${prefix}******`
    })
    // 脱敏 TC3 Authorization 签名与 SecretId
    masked = masked.replace(
      /(-H\s*["']Authorization:\s*TC3-HMAC-SHA256\s+Credential=)([^/]+)(\/[^"']*)(["'])/g,
      (_match, p1, secId, rest, p4) => {
        const maskedSecId = secId.length > 8 ? `${secId.slice(0, 4)}...${secId.slice(-4)}` : '****'
        return `${p1}${maskedSecId}${rest.replace(/Signature=[^"',\s]+/, 'Signature=******')}${p4}`
      }
    )
    return masked
  }, [effectiveCurlCommand])

  // 复制可执行的真实命令（含实时签名，用于终端脱机运行）
  const handleCopyExecutableCurl = () => {
    navigator.clipboard.writeText(effectiveCurlCommand)
    setCopiedCurl(true)
    setTimeout(() => setCopiedCurl(false), 2000)

    if (hasRealCredentials) {
      showToast({
        type: 'warning',
        message: '⚠️ 已复制包含管理员实时有效签名（UserSig）的可执行命令，可直接在终端脱机运行。凭据具备最高权限，请妥善保管勿外泄！',
      })
    } else if (effectiveCurlCommand.includes('USER_SIG_HERE')) {
      showToast({
        type: 'info',
        message: '已复制 cURL 模板。提示：点击「预览执行」或「发送」可自动注入实时签名。',
      })
    } else {
      showToast({
        type: 'success',
        message: '已复制 cURL 命令',
      })
    }
  }

  // 复制脱敏命令模板（用于分享、工单提交与归档）
  const handleCopyMaskedCurl = () => {
    navigator.clipboard.writeText(maskedCurlCommand)
    setCopiedCurl(true)
    setTimeout(() => setCopiedCurl(false), 2000)
    showToast({
      type: 'success',
      message: '已复制脱敏 cURL 模板（适合工单提交与公开分享）',
    })
  }

  const handleCopyCurl = handleCopyExecutableCurl

  // 计算应答报文大小
  const responseSize = useMemo(() => {
    if (!invokeResponse) return '0 B'
    try {
      const str = JSON.stringify(invokeResponse.response || invokeResponse)
      const bytes = new Blob([str]).size
      if (bytes > 1024) return (bytes / 1024).toFixed(1) + ' KB'
      return bytes + ' B'
    } catch {
      return '0 B'
    }
  }, [invokeResponse])

  // 根据当前 API 服务协议类型（腾讯云 API 3.0 vs 即时通信 IM REST）动态生成请求头表 (Headers)
  const effectiveHeaders = useMemo(() => {
    if (isApi3 && activeApi) {
      const endpoint = config.tc_endpoint || activeApi.endpoint || `${activeApi.service_name || 'live'}.tencentcloudapi.com`
      const action = activeApi.action || activeApi.command
      const version = activeApi.version || '2018-08-01'
      const region = config.tc_region || 'ap-guangzhou'
      const secId = config.tc_secret_id
        ? `${config.tc_secret_id.slice(0, 8)}...${config.tc_secret_id.slice(-4)}`
        : '未配置 (请在环境配置中填写)'

      return [
        {
          key: 'Content-Type',
          value: 'application/json; charset=utf-8',
          desc: '报文编码类型 (必选)。API 3.0 POST 统一采用 UTF-8 编码的 JSON 格式',
          required: true,
        },
        {
          key: 'Host',
          value: endpoint,
          desc: '目标服务器域名 (必选)。API 3.0 服务接入点',
          required: true,
        },
        {
          key: 'X-TC-Action',
          value: action,
          desc: `操作的接口名称 (必选)。例如 ${action}`,
          required: true,
        },
        {
          key: 'X-TC-Version',
          value: version,
          desc: `操作的 API 版本 (必选)。本产品当前固定为 ${version}`,
          required: true,
        },
        {
          key: 'X-TC-Timestamp',
          value: '$(date +%s) (当前 UNIX 秒级时间戳)',
          desc: '当前 UNIX 时间戳 (必选)。记录发起 API 请求的时间，与服务器时间差不能超过5分钟',
          required: true,
        },
        {
          key: 'X-TC-Region',
          value: region,
          desc: '地域参数 (根据接口要求传递)。用来标识希望操作哪个地域的数据',
          required: false,
        },
        {
          key: 'Authorization',
          value: `TC3-HMAC-SHA256 Credential=${secId}/..., SignedHeaders=content-type;host;x-tc-action, Signature=...`,
          desc: 'HTTP 标准身份认证头部 (必选)。由 TC3-HMAC-SHA256 签名算法根据密钥、时间戳与报文哈希自动计算',
          required: true,
        },
        {
          key: 'X-TC-Language',
          value: 'zh-CN',
          desc: '指定接口返回语言 (可选)。取值：zh-CN (中文) 或 en-US (英文)',
          required: false,
        },
      ]
    }

    // 默认即时通信 IM 服务端通信标准协议请求头
    return [
      {
        key: 'Content-Type',
        value: 'application/json;charset=utf-8',
        desc: '腾讯云 IM 要求必须为 UTF-8 编码的 JSON 报文',
        required: true,
      },
      {
        key: 'Accept',
        value: 'application/json',
        desc: '期望返回 JSON 格式结果',
        required: true,
      },
    ]
  }, [isApi3, activeApi, config])

  // 根据当前 API 服务协议类型动态生成 URL Query 参数表 (Params)
  const effectiveQueryParams = useMemo(() => {
    if (isApi3 && activeApi) {
      // 腾讯云 API 3.0 v3 规范：POST application/json 请求无 URL Query 参数，系统公共参数全部通过 Headers 传递
      return []
    }

    // 默认即时通信 IM 官方 5 大标准 URL Query 入参
    return [
      {
        key: 'sdkappid',
        value: String(config.sdkappid || 1400000000),
        desc: '腾讯云 IM 应用 SDKAppID',
        required: true,
      },
      {
        key: 'identifier',
        value: config.admin_user || 'administrator',
        desc: 'App 管理员用户账号',
        required: true,
      },
      {
        key: 'usersig',
        value: 'eJx1j... (系统自动根据密钥实时计算并附带)',
        desc: '管理员安全鉴权签名',
        required: true,
      },
      {
        key: 'random',
        value: '99999999 (32位无符号整数，实时随机生成)',
        desc: '请求随机数，用于防重放机制',
        required: true,
      },
      {
        key: 'contenttype',
        value: 'json',
        desc: '固定报文类型 (json)',
        required: true,
      },
    ]
  }, [isApi3, activeApi, config])

  if (tabs.length === 0 || !activeApi || !currentTab) {
    const recentApis = getRecentApis().slice(0, 6)
    const frequentApis = getFrequentApis().slice(0, 6)

    return (
      <div className="flex h-full w-full flex-col items-center justify-center bg-slate-50/70 dark:bg-slate-950/70 p-8 text-center text-slate-500 dark:text-slate-400 select-none overflow-y-auto">
        <div className="max-w-xl flex flex-col items-center space-y-5">
          <div className="flex h-16 w-16 items-center justify-center rounded-2xl bg-orange-100/70 dark:bg-orange-950/40 border border-orange-200/80 dark:border-orange-900/40 text-orange-600 dark:text-orange-400 shadow-2xs">
            <Terminal className="h-8 w-8" />
          </div>
          <div className="space-y-1.5">
            <h3 className="text-base font-bold text-slate-800 dark:text-slate-200">暂无打开的接口标签页</h3>
            <p className="text-xs text-slate-500 dark:text-slate-400 leading-relaxed max-w-md">
              支持类似 Postman / Apifox 的多标签页隔离调试。请从左侧接口导航树中点击任意接口以打开多标签调试，或从下方快捷方式打开常用或最近接口。
            </p>
          </div>

          {recentApis.length > 0 && (
            <div className="w-full flex flex-col items-center gap-2 pt-1">
              <div className="flex items-center gap-1.5 text-xs font-semibold text-slate-600 dark:text-slate-300">
                <Clock className="h-3.5 w-3.5 text-blue-500" />
                <span>最近打开</span>
              </div>
              <div className="flex flex-wrap items-center justify-center gap-2">
                {recentApis.map((api) => (
                  <Button
                    key={`recent-${api.api_id}`}
                    variant="outline"
                    size="sm"
                    onClick={() => openTab(api)}
                    className="h-8 text-xs bg-white dark:bg-slate-800 text-slate-700 dark:text-slate-200 hover:text-blue-600 dark:hover:text-blue-400 hover:border-blue-300 dark:hover:border-blue-600 border-slate-200 dark:border-slate-700 shadow-2xs gap-1.5"
                  >
                    <span className="text-[10px] px-1 py-0.5 rounded bg-blue-50 dark:bg-blue-950/50 text-blue-600 dark:text-blue-400 font-mono">
                      {api.subcategory || api.category}
                    </span>
                    <span>{getCleanApiName(api.name, api.command, api.action)}</span>
                  </Button>
                ))}
              </div>
            </div>
          )}

          <div className="w-full flex flex-col items-center gap-2 pt-1">
            <div className="flex items-center gap-1.5 text-xs font-semibold text-slate-600 dark:text-slate-300">
              <Sparkles className="h-3.5 w-3.5 text-orange-500" />
              <span>常用高频接口推荐</span>
            </div>
            <div className="flex flex-wrap items-center justify-center gap-2">
              {frequentApis.map((api) => (
                <Button
                  key={`freq-${api.api_id}`}
                  variant="outline"
                  size="sm"
                  onClick={() => openTab(api)}
                  className="h-8 text-xs bg-white dark:bg-slate-800 text-slate-700 dark:text-slate-200 hover:text-orange-600 dark:hover:text-orange-400 hover:border-orange-300 dark:hover:border-orange-600 border-slate-200 dark:border-slate-700 shadow-2xs gap-1.5"
                >
                  <Plus className="h-3 w-3 text-orange-500" />
                  <span>{getCleanApiName(api.name, api.command, api.action)}</span>
                </Button>
              ))}
            </div>
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="flex h-full w-full flex-col bg-white dark:bg-slate-900 overflow-hidden">
      {/* 0. Postman / Apifox 风格多标签栏 (Tabs Bar: 无滚动条、带新增与下拉搜索列表) */}
      <div className="relative flex items-center justify-between border-b border-slate-200 dark:border-slate-800 bg-slate-100/90 dark:bg-slate-950/80 px-2 pt-1.5 select-none shrink-0 gap-1">
        {/* Tabs List (自适应压缩宽度、无滚动条、激活项自动平滑聚焦) */}
        <div
          ref={tabsContainerRef}
          className="flex items-center gap-1 overflow-x-auto min-w-0 flex-1 [scrollbar-width:none] [-ms-overflow-style:none] [&::-webkit-scrollbar]:hidden scroll-smooth"
        >
          {tabs.map((tab) => {
            const isActive = tab.id === activeTabId
            const cleanTabName = getCleanApiName(tab.api.name, tab.api.command, tab.api.action)
            return (
              <div
                key={tab.id}
                ref={isActive ? activeTabRef : null}
                onClick={() => setActiveTabId(tab.id)}
                onContextMenu={(e) => handleTabContextMenu(e, tab)}
                title={`${cleanTabName} (${tab.api.command})`}
                className={`group relative flex items-center gap-1.5 h-8 px-2 rounded-t-lg text-xs transition-all cursor-pointer border-t-2 flex-1 shrink ${
                  isActive
                    ? 'min-w-[136px] max-w-[200px] bg-white dark:bg-slate-900 border-t-blue-500 text-slate-900 dark:text-slate-100 font-semibold shadow-2xs z-10'
                    : 'min-w-[118px] max-w-[180px] bg-slate-200/40 dark:bg-slate-900/40 hover:bg-slate-200/80 dark:hover:bg-slate-800/80 border-t-transparent text-slate-600 dark:text-slate-400 hover:text-slate-900 dark:hover:text-slate-200'
                }`}
              >
                <span
                  className={`font-mono text-[9px] font-semibold px-1 py-0.2 rounded shrink-0 transition-colors ${
                    isActive
                      ? 'bg-blue-50 dark:bg-blue-950 text-blue-600 dark:text-blue-400 border border-blue-200 dark:border-blue-800'
                      : 'bg-slate-200/70 dark:bg-slate-800 text-slate-500 dark:text-slate-400 border border-slate-300/50 dark:border-slate-700/50'
                  }`}
                >
                  POST
                </span>

                <span className="truncate flex-1 text-[11px] min-w-0" title={cleanTabName}>
                  {cleanTabName}
                </span>

                {tab.isModified && (
                  <span
                    title="草稿参数已变更"
                    className="h-1.5 w-1.5 rounded-full bg-amber-500 shrink-0"
                  />
                )}

                <button
                  type="button"
                  onClick={(e) => {
                    e.stopPropagation()
                    closeTab(tab.id)
                  }}
                  title="关闭标签页"
                  className={`rounded-full p-0.5 text-slate-400 dark:text-slate-500 hover:text-slate-700 dark:hover:text-slate-200 hover:bg-slate-200/80 dark:hover:bg-slate-750 transition-all cursor-pointer shrink-0 ml-auto ${
                    isActive ? 'block opacity-70 hover:opacity-100' : 'hidden group-hover:block'
                  }`}
                >
                  <X className="h-3 w-3" />
                </button>
              </div>
            )
          })}

          {/* 新增标签按钮 (+) */}
          <button
            type="button"
            onClick={() => {
              setShowTabDropdown(true)
              setTimeout(() => searchInputRef.current?.focus(), 50)
            }}
            title="新建标签页 / 快捷打开接口"
            className="flex items-center justify-center h-7 w-7 rounded-md text-slate-500 dark:text-slate-400 hover:text-blue-600 dark:hover:text-blue-400 hover:bg-slate-200/80 dark:hover:bg-slate-800 transition-colors cursor-pointer shrink-0 ml-0.5 active:scale-95"
          >
            <Plus className="h-4 w-4" />
          </button>
        </div>

        {/* Tab 下拉展开按钮 (弹出可搜索的标签与全部接口列表) */}
        <div className="relative flex items-center shrink-0 pl-1 pb-1">
          <button
            type="button"
            onClick={() => {
              setShowTabDropdown(!showTabDropdown)
              if (!showTabDropdown) {
                setTimeout(() => searchInputRef.current?.focus(), 50)
              }
            }}
            title="已打开标签列表与全局搜索"
            className={`flex items-center gap-1.5 h-7 px-2 rounded-lg border transition-all cursor-pointer text-xs font-semibold ${
              showTabDropdown
                ? 'bg-white dark:bg-slate-800 text-blue-600 dark:text-blue-400 border-blue-300 dark:border-blue-500 shadow-2xs'
                : 'bg-white/80 dark:bg-slate-850 border-slate-200 dark:border-slate-750 text-slate-600 dark:text-slate-300 hover:text-slate-900 dark:hover:text-white hover:bg-white dark:hover:bg-slate-800 hover:border-slate-300 dark:hover:border-slate-600'
            }`}
          >
            <span className="font-mono text-[11px] font-bold text-slate-500 dark:text-slate-400">{tabs.length}</span>
            <ChevronDown
              className={`h-3.5 w-3.5 transition-transform duration-150 ${
                showTabDropdown ? 'rotate-180 text-blue-600 dark:text-blue-400' : 'text-slate-400 dark:text-slate-500'
              }`}
            />
          </button>

          {/* 下拉弹出可搜索的标签列表与全部接口 (Apifox / Postman 风格) */}
          <AnimatePresence>
            {showTabDropdown && (
              <motion.div
                ref={dropdownRef}
                initial={{ opacity: 0, y: -4, scale: 0.98 }}
                animate={{ opacity: 1, y: 0, scale: 1 }}
                exit={{ opacity: 0, y: -4, scale: 0.98 }}
                transition={{ duration: 0.12 }}
                className="absolute top-9 right-0 z-50 w-80 max-w-[calc(100vw-24px)] max-h-[460px] flex flex-col rounded-xl border border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900 shadow-xl shadow-slate-900/10 dark:shadow-slate-950/60 overflow-hidden"
              >
                {/* 搜索框 */}
                <div className="p-2 border-b border-slate-100 dark:border-slate-800 bg-slate-50/80 dark:bg-slate-950/60 flex items-center gap-1.5">
                  <Search className="h-3.5 w-3.5 text-slate-400 shrink-0 ml-1" />
                  <input
                    ref={searchInputRef}
                    value={tabSearchQuery}
                    onChange={(e) => setTabSearchQuery(e.target.value)}
                    placeholder={`搜索标签页或全量 ${apis.length || '所有'} 个接口...`}
                    className="w-full bg-transparent text-xs text-slate-800 dark:text-slate-200 placeholder:text-slate-400 dark:placeholder:text-slate-500 focus:outline-none"
                  />
                  {tabSearchQuery && (
                    <button
                      onClick={() => setTabSearchQuery('')}
                      className="text-slate-400 hover:text-slate-600 dark:hover:text-slate-200 p-0.5 cursor-pointer"
                    >
                      <X className="h-3 w-3" />
                    </button>
                  )}
                </div>

                {/* 列表内容区 */}
                <div className="flex-1 overflow-y-auto p-1.5 space-y-2.5 [scrollbar-width:none]">
                  {/* 已打开的标签页列表 */}
                  <div>
                    <div className="flex items-center justify-between px-2 py-1 text-[10px] font-bold text-slate-400 dark:text-slate-500 uppercase tracking-wider">
                      <span>已打开标签页</span>
                      <span className="font-mono">{filteredOpenTabs.length}</span>
                    </div>
                    {filteredOpenTabs.length === 0 ? (
                      <div className="text-center py-2 text-[11px] text-slate-400 dark:text-slate-500">
                        无匹配的已打开标签
                      </div>
                    ) : (
                      <div className="space-y-0.5">
                        {filteredOpenTabs.map((t) => {
                          const isActive = t.id === activeTabId
                          return (
                            <div
                              key={t.id}
                              onClick={() => {
                                setActiveTabId(t.id)
                                setShowTabDropdown(false)
                              }}
                              className={`group flex items-center justify-between px-2.5 py-1.5 rounded-lg text-xs cursor-pointer transition-colors ${
                                isActive
                                  ? 'bg-blue-50/80 dark:bg-blue-950/40 text-blue-900 dark:text-blue-200 font-semibold border-l-2 border-l-blue-500'
                                  : 'text-slate-700 dark:text-slate-300 hover:bg-slate-100 dark:hover:bg-slate-800'
                              }`}
                            >
                              <div className="flex items-center gap-1.5 min-w-0 flex-1">
                                <span className="font-mono text-[9px] font-semibold text-slate-500 dark:text-slate-400 bg-slate-100 dark:bg-slate-800 border border-slate-200 dark:border-slate-700 px-1 py-0.2 rounded shrink-0">
                                  POST
                                </span>
                                <span className="truncate text-xs">{getCleanApiName(t.api.name, t.api.command, t.api.action)}</span>
                                {t.isModified && (
                                  <span className="h-1.5 w-1.5 rounded-full bg-amber-500 shrink-0" title="已修改" />
                                )}
                              </div>
                              <div className="flex items-center gap-1 shrink-0 ml-1">
                                {isActive && (
                                  <span className="text-[10px] text-blue-600 dark:text-blue-400 bg-blue-100/60 dark:bg-blue-950/50 px-1.5 py-0.2 rounded font-mono">
                                    当前
                                  </span>
                                )}
                                <button
                                  type="button"
                                  onClick={(e) => {
                                    e.stopPropagation()
                                    closeTab(t.id)
                                  }}
                                  title="关闭标签页"
                                  className="p-1 text-slate-400 hover:text-rose-600 rounded-md hover:bg-slate-200/60 dark:hover:bg-slate-700 opacity-60 group-hover:opacity-100 transition-opacity cursor-pointer"
                                >
                                  <X className="h-3 w-3" />
                                </button>
                              </div>
                            </div>
                          )
                        })}
                      </div>
                    )}
                  </div>

                  {/* 其它接口快捷打开 */}
                  {availableApis.length > 0 && (
                    <div className="border-t border-slate-100 dark:border-slate-800 pt-1.5">
                      <div className="flex items-center justify-between px-2 py-1 text-[10px] font-bold text-slate-400 dark:text-slate-500 uppercase tracking-wider">
                        <span>{tabSearchQuery ? '搜索结果接口' : '快捷打开其它接口'}</span>
                        <span className="font-mono">{availableApis.length}</span>
                      </div>
                      <div className="space-y-0.5 max-h-48 overflow-y-auto [scrollbar-width:none]">
                        {availableApis.slice(0, 30).map((api) => (
                          <div
                            key={api.api_id}
                            onClick={() => {
                              openTab(api)
                              setShowTabDropdown(false)
                            }}
                            className="flex items-center justify-between px-2.5 py-1.5 rounded-lg text-xs cursor-pointer text-slate-700 dark:text-slate-300 hover:bg-blue-50/80 dark:hover:bg-blue-950/40 hover:text-blue-900 dark:hover:text-blue-200 transition-colors group"
                          >
                            <div className="min-w-0 flex-1">
                              <div className="flex items-center gap-1.5">
                                <span className="font-mono text-[9px] font-semibold text-slate-400 dark:text-slate-500 group-hover:text-blue-600 dark:group-hover:text-blue-400">
                                  [{api.category}]
                                </span>
                                <span className="truncate text-xs font-medium">{getCleanApiName(api.name, api.command, api.action)}</span>
                              </div>
                              <p className="font-mono text-[10px] text-slate-400 dark:text-slate-500 truncate pl-0.5">
                                {api.command}
                              </p>
                            </div>
                            <Plus className="h-3.5 w-3.5 text-slate-300 dark:text-slate-600 group-hover:text-blue-600 dark:group-hover:text-blue-400 shrink-0 ml-1.5" />
                          </div>
                        ))}
                      </div>
                    </div>
                  )}
                </div>
              </motion.div>
            )}
          </AnimatePresence>
        </div>
      </div>

      {/* 1. Apifox / Postman 经典顶部标题与操作栏 */}
      <div className="flex flex-col border-b border-slate-200/90 dark:border-slate-800 bg-white dark:bg-slate-900 px-3 sm:px-5 pt-3 pb-2.5 shadow-2xs gap-2 sm:gap-2.5">
        {/* 面包屑与快捷动作 */}
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex items-center gap-2 min-w-0 flex-1 truncate">
            {(() => {
              let parts = (activeApi.path && activeApi.path.length > 0)
                ? [...activeApi.path]
                : [activeApi.category, activeApi.subcategory].filter(Boolean) as string[]
              if (parts.length > 1 && (parts[0].includes('云直播') || parts[0].includes('CSS') || parts[0] === '即时通信 IM')) {
                parts = parts.slice(1)
              }
              const cleanName = getCleanApiName(activeApi.name, activeApi.command, activeApi.action)
              return (
                <div className="flex items-center gap-1.5 text-xs text-slate-500 dark:text-slate-400 font-medium truncate min-w-0">
                  {parts.length > 0 && (
                    <span className="hidden sm:inline-flex items-center gap-1 truncate text-slate-400">
                      {parts.map((seg, idx) => (
                        <React.Fragment key={idx}>
                          <span className="truncate">{seg}</span>
                          <span>/</span>
                        </React.Fragment>
                      ))}
                    </span>
                  )}
                  <h2 className="text-xs font-bold text-slate-900 dark:text-slate-100 tracking-tight truncate">
                    {cleanName}
                  </h2>
                </div>
              )
            })()}
            {activeApi.doc_id && (
              <button
                type="button"
                onClick={() => {
                  navigator.clipboard.writeText(String(activeApi.doc_id))
                  showToast({
                    type: 'success',
                    message: `已复制文档 ID: ${activeApi.doc_id}`,
                  })
                }}
                className="font-mono text-[10px] text-slate-500 dark:text-slate-400 bg-slate-100 dark:bg-slate-800 hover:bg-slate-200 dark:hover:bg-slate-700 px-1.5 py-0.5 rounded border border-slate-200/70 dark:border-slate-750 cursor-pointer flex items-center gap-1 transition-colors shrink-0"
                title="点击复制文档 ID"
              >
                <span>#{activeApi.doc_id}</span>
                <Copy className="h-2.5 w-2.5 opacity-60" />
              </button>
            )}
          </div>

          <div className="flex items-center gap-1.5 shrink-0 overflow-x-auto">
            {/* 复制 cURL 命令 */}
            <Button
              variant="outline"
              size="sm"
              onClick={handleCopyCurl}
              className="h-7 gap-1 text-[11px] font-semibold text-slate-700 dark:text-slate-300 hover:text-blue-600 dark:hover:text-blue-400 border-slate-200 dark:border-slate-750 hover:border-blue-300 dark:hover:border-blue-600 bg-white dark:bg-slate-800 shrink-0"
              title={
                hasRealCredentials
                  ? '复制含管理员实时有效凭据的完整可执行命令（可直接在终端脱机运行）'
                  : '生成并复制该接口的 cURL 脚本'
              }
            >
              {copiedCurl ? (
                <Check className="h-3 w-3 text-emerald-600 dark:text-emerald-400" />
              ) : (
                <Terminal className="h-3 w-3" />
              )}
              <span>{copiedCurl ? '已复制' : hasRealCredentials ? '复制可执行 cURL' : 'cURL'}</span>
            </Button>

            {/* 以此接口建批量调用 */}
            <Button
              variant="outline"
              size="sm"
              onClick={handleSetBatch}
              className="h-7 gap-1 text-[11px] font-semibold text-slate-700 dark:text-slate-300 hover:text-blue-600 dark:hover:text-blue-400 border-slate-200 dark:border-slate-750 hover:border-blue-300 dark:hover:border-blue-600 bg-white dark:bg-slate-800 shrink-0"
              title="将此接口设为批量任务中心目标"
            >
              <Layers className="h-3 w-3" />
              <span>批量调用</span>
            </Button>

            {/* 官方文档跳转 */}
            {activeApi.doc_url && (
              <a
                href={activeApi.doc_url}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-1 h-7 px-2.5 rounded-lg border border-slate-200 dark:border-slate-750 text-[11px] font-semibold text-slate-600 dark:text-slate-300 hover:text-blue-600 dark:hover:text-blue-400 hover:border-blue-300 dark:hover:border-blue-600 bg-white dark:bg-slate-800 shadow-2xs transition-colors shrink-0"
                title="在新标签页中打开腾讯云官方文档"
              >
                <span>文档</span>
                <ExternalLink className="h-3 w-3" />
              </a>
            )}
          </div>
        </div>

        {/* 2. 标志性 Postman / Apifox 请求地址栏 (URL Bar) + 预览模式开关 */}
        <div className="flex flex-col sm:flex-row items-stretch sm:items-center gap-2">
          {/* 统一地址输入控件 */}
          <div className="flex-1 flex items-center rounded-xl border border-slate-300/90 dark:border-slate-750 bg-white dark:bg-slate-925 shadow-2xs hover:border-slate-400 dark:hover:border-slate-600 focus-within:border-blue-500 focus-within:ring-2 focus-within:ring-blue-500/15 transition-all overflow-hidden min-w-0">
            {/* HTTP Method Pill */}
            <div className="flex items-center gap-1 bg-orange-500/10 dark:bg-orange-500/15 text-orange-600 dark:text-orange-400 font-mono font-bold text-xs px-2.5 sm:px-3 py-1.5 border-r border-slate-200 dark:border-slate-750 select-none shrink-0">
              <span>POST</span>
              <ChevronDown className="h-3 w-3 text-orange-400" />
            </div>

            {/* URL Endpoint Display */}
            <div className="flex-1 flex items-center px-2.5 sm:px-3 py-1.5 font-mono text-xs text-slate-800 dark:text-slate-200 truncate min-w-0">
              <span className="text-slate-400 dark:text-slate-500 select-none">https://</span>
              <span className="font-semibold text-slate-700 dark:text-slate-300 truncate">{endpointHost}</span>
              <span className="text-slate-400 dark:text-slate-500 select-none">/</span>
              {!isApi3 && (
                <span className="text-blue-600 dark:text-blue-400 font-semibold truncate">{fullCommand}</span>
              )}
              {isApi3 && (
                <span className="inline-flex items-center gap-1.5 ml-2 font-mono text-[10px] px-2 py-0.5 rounded bg-emerald-50 dark:bg-emerald-950/40 text-emerald-700 dark:text-emerald-400 border border-emerald-200 dark:border-emerald-800/60 font-semibold shrink-0">
                  <span className="opacity-75">Action:</span>
                  <span className="font-bold">{activeApi?.action || activeApi?.command}</span>
                </span>
              )}
            </div>

            {/* 快速复制接口完整 URL */}
            <button
              type="button"
              onClick={() => {
                navigator.clipboard.writeText(fullUrl)
                showToast({
                  type: 'success',
                  message: `已复制接口 URL: ${fullUrl}`,
                })
              }}
              className="mr-1.5 p-1 rounded-md text-slate-400 hover:text-blue-600 dark:hover:text-blue-400 hover:bg-slate-100 dark:hover:bg-slate-800 transition-colors cursor-pointer shrink-0"
              title="复制接口完整 URL"
            >
              <Copy className="h-3.5 w-3.5" />
            </button>

            {/* Header/Params Indicator Pill */}
            <button
              onClick={() => handleSetRequestTab(isApi3 ? 'headers' : 'params')}
              className="mr-2 hidden sm:flex items-center gap-1 px-2 py-0.5 rounded-md bg-slate-100 dark:bg-slate-800 hover:bg-slate-200 dark:hover:bg-slate-750 text-slate-600 dark:text-slate-300 font-mono text-[11px] border border-slate-200/60 dark:border-slate-750 transition-colors cursor-pointer shrink-0"
              title={isApi3 ? '查看与配置 API 3.0 标准 Headers' : '查看与配置 Query 参数'}
            >
              {isApi3 ? (
                <>
                  <span className="text-emerald-600 dark:text-emerald-400 font-bold">H</span>
                  <span>{effectiveHeaders.length} headers</span>
                </>
              ) : (
                <>
                  <span className="text-blue-600 dark:text-blue-400 font-bold">?</span>
                  <span>{effectiveQueryParams.length} params</span>
                </>
              )}
            </button>
          </div>

          <div className="flex items-center gap-2 justify-end shrink-0">
            {/* 开启 / 关闭预览模式切换开关 (Dry Run Toggle) */}
            <button
              type="button"
              onClick={togglePreviewMode}
              className={`flex-1 sm:flex-initial flex items-center justify-center gap-1.5 h-9 px-3 rounded-xl border text-xs font-semibold transition-all cursor-pointer select-none active:scale-95 shrink-0 ${
                isPreviewMode
                  ? 'bg-violet-50 dark:bg-violet-950/50 text-violet-700 dark:text-violet-300 border-violet-300 dark:border-violet-700 ring-2 ring-violet-500/20 shadow-2xs'
                  : 'bg-slate-50 dark:bg-slate-850 hover:bg-slate-100 dark:hover:bg-slate-800 text-slate-600 dark:text-slate-300 border-slate-200 dark:border-slate-700'
              }`}
              title={
                isPreviewMode
                  ? '【预览模式已开启】执行时不发真实网络请求，点击切换为线上真实发包'
                  : '【真实请求模式】点击开启预览模式（仅校验并生成报文，不发网络请求）'
              }
            >
              {isPreviewMode ? (
                <Eye className="h-3.5 w-3.5 text-violet-600 dark:text-violet-400 animate-pulse shrink-0" />
              ) : (
                <EyeOff className="h-3.5 w-3.5 text-slate-400 dark:text-slate-500 shrink-0" />
              )}
              <span className="whitespace-nowrap">{isPreviewMode ? '预览模式 (开启)' : '预览模式'}</span>
              <span
                className={`h-2 w-2 rounded-full transition-colors shrink-0 ${
                  isPreviewMode ? 'bg-violet-600 shadow-xs shadow-violet-500' : 'bg-slate-300 dark:bg-slate-600'
                }`}
              />
            </button>

            {/* 经典 Apifox / Postman 发送大按钮 (依据预览模式自适应样式) */}
            <Button
              size="sm"
              onClick={handleSend}
              disabled={isInvoking || (bodyMode === 'json' && !!jsonError)}
              className={`flex-1 sm:flex-initial h-9 px-4 sm:px-5 rounded-xl font-bold text-xs gap-1.5 cursor-pointer active:scale-95 transition-all text-white shrink-0 ${
                isPreviewMode
                  ? 'bg-violet-600 hover:bg-violet-700 active:bg-violet-800 shadow-xs shadow-violet-500/20'
                  : 'bg-orange-500 hover:bg-orange-600 active:bg-orange-700 shadow-xs shadow-orange-500/20'
              }`}
              title={
                isPreviewMode
                  ? '【预览执行】参数校验与报文构建，不向服务端发起真实网络请求 (⌘↵)'
                  : '【发送请求】向腾讯云服务端发起真实 REST API 调用 (⌘↵)'
              }
            >
              {isPreviewMode ? (
                <Sparkles className={`h-3.5 w-3.5 shrink-0 ${isInvoking ? 'animate-spin' : ''}`} />
              ) : (
                <Send className={`h-3.5 w-3.5 shrink-0 ${isInvoking ? 'animate-bounce' : ''}`} />
              )}
              <span className="whitespace-nowrap">
                {isInvoking
                  ? isPreviewMode
                    ? '校验中...'
                    : '调用中...'
                  : isPreviewMode
                  ? '预览执行'
                  : '发送'}
              </span>
              <kbd
                className={`hidden sm:inline-block ml-1 font-mono text-[10px] px-1 rounded ${
                  isPreviewMode ? 'bg-violet-700/60 text-violet-100' : 'bg-orange-600/60 text-orange-100'
                }`}
              >
                ⌘↵
              </kbd>
            </Button>
          </div>
        </div>

        {/* 预览模式生效中的安全提示横条 */}
        {isPreviewMode && (
          <div className="flex items-center justify-between rounded-lg bg-violet-50/90 dark:bg-violet-950/60 border border-violet-200/90 dark:border-violet-800/80 px-3 py-1.5 text-xs text-violet-800 dark:text-violet-200">
            <div className="flex items-center gap-2">
              <span className="flex h-2 w-2 relative">
                <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-violet-400 opacity-75"></span>
                <span className="relative inline-flex rounded-full h-2 w-2 bg-violet-600"></span>
              </span>
              <span>
                <strong>预览模式已开启</strong>：点击「预览执行」将仅在本地校验入参并构建请求报文，<strong>绝对不发网络请求</strong>，可安全演练。
              </span>
            </div>
            <button
              onClick={() => setPreviewMode(false)}
              className="text-[11px] text-violet-600 dark:text-violet-400 hover:text-violet-900 dark:hover:text-violet-100 underline font-semibold ml-2 cursor-pointer"
            >
              关闭预览模式
            </button>
          </div>
        )}
      </div>

      {/* 3. Resizable 上下双窗格（上为请求编辑器，下为响应检查器） */}
      <div className="flex-1 overflow-hidden relative flex flex-col">
        <Group groupRef={workbenchGroupRef} orientation="vertical" className="flex-1 min-h-0">
          {/* 上窗格：Request Editor */}
          <Panel id="request-panel" defaultSize={52} minSize={20}>
            <div className="flex h-full flex-col bg-white dark:bg-slate-900 overflow-hidden">
              {/* Request Tabs Header */}
              <div className="flex flex-wrap items-center justify-between border-b border-slate-200 dark:border-slate-800 bg-slate-50/60 dark:bg-slate-950/50 px-2 sm:px-4 gap-y-1">
                <div className="flex items-center gap-1 -mb-px overflow-x-auto scrollbar-none py-0.5">
                  <button
                    onClick={() => handleSetRequestTab('params')}
                    className={`flex items-center gap-1.5 px-2.5 sm:px-3 py-2 text-xs font-semibold border-b-2 transition-all cursor-pointer shrink-0 whitespace-nowrap ${
                      requestTab === 'params'
                        ? 'border-blue-500 text-blue-600 dark:text-blue-400 bg-white dark:bg-slate-900 font-bold'
                        : 'border-transparent text-slate-600 dark:text-slate-400 hover:text-slate-900 dark:hover:text-slate-200'
                    }`}
                  >
                    <span>Params</span>
                    <span className="font-mono text-[10px] text-slate-400 dark:text-slate-500 bg-slate-200/60 dark:bg-slate-800 px-1 rounded-full">
                      {effectiveQueryParams.length}
                    </span>
                  </button>

                  <button
                    onClick={() => handleSetRequestTab('body')}
                    className={`flex items-center gap-1.5 px-2.5 sm:px-3 py-2 text-xs font-semibold border-b-2 transition-all cursor-pointer shrink-0 whitespace-nowrap ${
                      requestTab === 'body'
                        ? 'border-blue-500 text-blue-600 dark:text-blue-400 bg-white dark:bg-slate-900 font-bold'
                        : 'border-transparent text-slate-600 dark:text-slate-400 hover:text-slate-900 dark:hover:text-slate-200'
                    }`}
                  >
                    <span>Body</span>
                    <span className="font-mono text-[10px] text-emerald-600 dark:text-emerald-400 bg-emerald-50 dark:bg-emerald-950/60 px-1 rounded font-semibold">
                      json
                    </span>
                  </button>

                  <button
                    onClick={() => handleSetRequestTab('headers')}
                    className={`flex items-center gap-1.5 px-2.5 sm:px-3 py-2 text-xs font-semibold border-b-2 transition-all cursor-pointer shrink-0 whitespace-nowrap ${
                      requestTab === 'headers'
                        ? 'border-blue-500 text-blue-600 dark:text-blue-400 bg-white dark:bg-slate-900 font-bold'
                        : 'border-transparent text-slate-600 dark:text-slate-400 hover:text-slate-900 dark:hover:text-slate-200'
                    }`}
                  >
                    <span>Headers</span>
                    <span
                      className={`font-mono text-[10px] px-1 rounded-full ${
                        isApi3
                          ? 'text-emerald-700 dark:text-emerald-300 bg-emerald-100 dark:bg-emerald-950/60 font-semibold'
                          : 'text-slate-400 dark:text-slate-500 bg-slate-200/60 dark:bg-slate-800'
                      }`}
                    >
                      {effectiveHeaders.length}
                    </span>
                  </button>

                  <button
                    onClick={() => handleSetRequestTab('docs')}
                    className={`flex items-center gap-1.5 px-2.5 sm:px-3 py-2 text-xs font-semibold border-b-2 transition-all cursor-pointer shrink-0 whitespace-nowrap ${
                      requestTab === 'docs'
                        ? 'border-blue-500 text-blue-600 dark:text-blue-400 bg-white dark:bg-slate-900 font-bold'
                        : 'border-transparent text-slate-600 dark:text-slate-400 hover:text-slate-900 dark:hover:text-slate-200'
                    }`}
                  >
                    <Info className="h-3 w-3" />
                    <span>接口文档</span>
                  </button>
                </div>

                {/* Body Tab 专属工具操作栏 */}
                {requestTab === 'body' && (
                  <div className="flex items-center gap-2 py-1">
                    {/* Visual Form / JSON 模式切换 */}
                    <div className="flex items-center rounded-lg bg-slate-200/70 dark:bg-slate-800 p-0.5 text-xs font-medium">
                      <button
                        onClick={() => handleSetBodyMode('form')}
                        className={`flex items-center gap-1 px-2.5 py-0.5 rounded-md text-[11px] font-semibold transition-all cursor-pointer ${
                          bodyMode === 'form'
                            ? 'bg-white dark:bg-slate-700 text-slate-900 dark:text-slate-100 shadow-2xs'
                            : 'text-slate-600 dark:text-slate-400 hover:text-slate-900 dark:hover:text-slate-200'
                        }`}
                      >
                        <FileText className="h-3 w-3" />
                        <span>表单模式</span>
                      </button>
                      <button
                        onClick={() => handleSetBodyMode('json')}
                        className={`flex items-center gap-1 px-2.5 py-0.5 rounded-md text-[11px] font-semibold transition-all cursor-pointer ${
                          bodyMode === 'json'
                            ? 'bg-white dark:bg-slate-700 text-slate-900 dark:text-slate-100 shadow-2xs'
                            : 'text-slate-600 dark:text-slate-400 hover:text-slate-900 dark:hover:text-slate-200'
                        }`}
                      >
                        <Code className="h-3 w-3" />
                        <span>JSON (Raw)</span>
                      </button>
                    </div>

                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={handleReset}
                      className="h-6 px-2 text-[11px] text-slate-500 dark:text-slate-400 hover:text-slate-800 dark:hover:text-slate-200"
                      title="重置为官方示例参数"
                    >
                      <RotateCcw className="h-2.5 w-2.5 mr-1" />
                      <span>重置</span>
                    </Button>
                  </div>
                )}
              </div>

              {/* Request Tab Contents */}
              <div className="flex-1 overflow-y-auto p-4">
                {/* 1. Params Tab (URL Query 参数) */}
                {requestTab === 'params' && (
                  <div className="space-y-4">
                    {isApi3 ? (
                      <div className="rounded-xl border border-blue-200/80 dark:border-blue-900/50 bg-blue-50/40 dark:bg-blue-950/20 p-4 space-y-3">
                        <div className="flex items-start gap-3">
                          <div className="p-1.5 rounded-lg bg-blue-100 dark:bg-blue-900/60 text-blue-600 dark:text-blue-400 mt-0.5 shrink-0">
                            <Info className="h-4 w-4" />
                          </div>
                          <div className="space-y-1.5 flex-1 min-w-0">
                            <h4 className="text-xs font-semibold text-slate-800 dark:text-slate-200 flex items-center gap-2">
                              <span>腾讯云 API 3.0 (v3 签名) 规范说明</span>
                              <span className="font-mono text-[10px] px-1.5 py-0.2 rounded bg-blue-100 dark:bg-blue-900/60 text-blue-700 dark:text-blue-300 font-normal">
                                POST application/json
                              </span>
                            </h4>
                            <p className="text-xs text-slate-600 dark:text-slate-400 leading-relaxed">
                              依据腾讯云官方文档《
                              <a
                                href="https://cloud.tencent.com/document/api/267/20459"
                                target="_blank"
                                rel="noreferrer"
                                className="text-blue-600 dark:text-blue-400 hover:underline font-medium inline-flex items-center gap-0.5"
                              >
                                <span>公共参数 (文档 20459)</span>
                                <ExternalLink className="h-2.5 w-2.5" />
                              </a>
                              》，使用推荐的签名方法 v3 (TC3-HMAC-SHA256) 发起 POST 请求时：
                            </p>
                            <ul className="text-xs text-slate-600 dark:text-slate-400 space-y-1.5 list-disc list-inside">
                              <li>
                                <strong className="text-slate-700 dark:text-slate-300 font-semibold">系统公共参数</strong>：必须统一放置在 <strong className="text-emerald-600 dark:text-emerald-400 font-semibold">HTTP Headers</strong> 请求头中传递（包括 <code className="px-1 py-0.5 rounded bg-slate-200/60 dark:bg-slate-800 text-[11px] font-mono">X-TC-Action</code>、<code className="px-1 py-0.5 rounded bg-slate-200/60 dark:bg-slate-800 text-[11px] font-mono">X-TC-Version</code>、<code className="px-1 py-0.5 rounded bg-slate-200/60 dark:bg-slate-800 text-[11px] font-mono">X-TC-Region</code>、<code className="px-1 py-0.5 rounded bg-slate-200/60 dark:bg-slate-800 text-[11px] font-mono">Authorization</code> 等）。
                              </li>
                              <li>
                                <strong className="text-slate-700 dark:text-slate-300 font-semibold">URL Query 参数</strong>：POST application/json 请求结构下 <strong className="text-slate-700 dark:text-slate-300 font-semibold">无需传递 Query 参数</strong>，URL 统一指向服务根路径 <code className="px-1 py-0.5 rounded bg-slate-200/60 dark:bg-slate-800 text-[11px] font-mono">https://{endpointHost}/</code>。
                              </li>
                              <li>
                                <strong className="text-slate-700 dark:text-slate-300 font-semibold">业务入参</strong>：统一在 <strong className="text-emerald-600 dark:text-emerald-400 font-semibold">Body</strong> 标签页中传递 JSON 数据。
                              </li>
                            </ul>
                            <p className="text-[11px] text-slate-400 dark:text-slate-500 pt-1">
                              * 注：仅在已淘汰的旧版 v1 签名或 HTTP GET 结构中，公共参数才作为 URL Query 携带。
                            </p>
                          </div>
                        </div>

                        <div className="pt-2 border-t border-blue-200/50 dark:border-blue-900/30 flex items-center justify-between text-xs">
                          <span className="text-slate-500 dark:text-slate-400">
                            查看公共请求头规范与各字段值：
                          </span>
                          <button
                            type="button"
                            onClick={() => handleSetRequestTab('headers')}
                            className="inline-flex items-center gap-1 font-semibold text-blue-600 dark:text-blue-400 hover:text-blue-700 cursor-pointer"
                          >
                            <span>前往 Headers 标签页查看 ({effectiveHeaders.length} 项)</span>
                            <ChevronRight className="h-3.5 w-3.5" />
                          </button>
                        </div>
                      </div>
                    ) : (
                      <>
                        <p className="text-[11px] text-slate-400 dark:text-slate-500">
                          Query 参数将在请求时由系统根据配置自动注入到 URL 后缀中：
                        </p>
                        <div className="rounded-xl border border-slate-200 dark:border-slate-800 overflow-x-auto bg-white dark:bg-slate-900 shadow-2xs">
                          <table className="w-full text-left text-xs">
                            <thead className="bg-slate-50 dark:bg-slate-850 border-b border-slate-200 dark:border-slate-800 text-slate-500 dark:text-slate-400 font-semibold">
                              <tr>
                                <th className="py-2 px-3 w-12 text-center">状态</th>
                                <th className="py-2 px-3 w-36">参数名 (Key)</th>
                                <th className="py-2 px-3">参数值 (Value)</th>
                                <th className="py-2 px-3">说明 (Description)</th>
                              </tr>
                            </thead>
                            <tbody className="divide-y divide-slate-100 dark:divide-slate-800 font-mono text-[11px]">
                              {effectiveQueryParams.map((p) => (
                                <tr key={p.key} className="hover:bg-slate-50/60 dark:hover:bg-slate-850/60">
                                  <td className="py-2 px-3 text-center">
                                    <input
                                      type="checkbox"
                                      checked={true}
                                      readOnly
                                      className="rounded border-slate-300 dark:border-slate-600 text-orange-500 opacity-75"
                                    />
                                  </td>
                                  <td className="py-2 px-3 font-semibold text-slate-800 dark:text-slate-200">
                                    {p.key}
                                  </td>
                                  <td className="py-2 px-3 text-blue-600 dark:text-blue-400 truncate max-w-xs font-medium">
                                    {p.value}
                                  </td>
                                  <td className="py-2 px-3 text-slate-500 dark:text-slate-400 font-sans text-xs">
                                    {p.desc}
                                  </td>
                                </tr>
                              ))}
                            </tbody>
                          </table>
                        </div>
                      </>
                    )}
                  </div>
                )}

                {/* 2. Body Tab */}
                {requestTab === 'body' && (
                  <div>
                    {bodyMode === 'form' ? (
                      <div className="grid grid-cols-1 md:grid-cols-2 gap-3.5">
                        {effectiveFields && effectiveFields.length > 0 ? (
                          effectiveFields.map((field) => {
                            const category = getFieldTypeCategory(
                              field.type,
                              formData[field.name]
                            )
                            const isComplexList =
                              category === 'list' &&
                              isComplexListField(field, formData[field.name])
                            const isPredefined = (activeApi?.fields || []).some(
                              (f) => f.name === field.name
                            )
                            const isFullWidth =
                              category === 'json' ||
                              category === 'list' ||
                              field.name === 'Text' ||
                              field.name === 'Introduction' ||
                              field.name === 'Notification'

                            return (
                              <div
                                key={field.name}
                                className={`flex flex-col gap-1.5 ${
                                  isFullWidth ? 'col-span-1 md:col-span-2' : 'col-span-1'
                                }`}
                              >
                                <div className="flex items-center justify-between text-xs">
                                  <label className="font-semibold text-slate-700 dark:text-slate-300 flex items-center gap-1.5">
                                    <span>{field.label || field.name}</span>
                                    {field.required && (
                                      <span className="text-rose-500 font-bold">*</span>
                                    )}
                                    <span className="font-mono text-[10px] text-slate-400 dark:text-slate-500 font-normal">
                                      ({field.name})
                                    </span>
                                    {!isPredefined && (
                                      <span className="font-mono text-[9px] text-orange-600 dark:text-orange-400 bg-orange-500/10 dark:bg-orange-500/15 border border-orange-200/80 dark:border-orange-500/30 px-1 py-0.2 rounded font-medium">
                                        动态/示例字段
                                      </span>
                                    )}
                                  </label>

                                  {/* 类型徽标 */}
                                  {category === 'boolean' && (
                                    <span className="font-mono text-[10px] text-emerald-700 dark:text-emerald-400 bg-emerald-50 dark:bg-emerald-950/40 border border-emerald-200 dark:border-emerald-800/60 px-1.5 py-0.5 rounded font-medium">
                                      boolean
                                    </span>
                                  )}
                                  {category === 'number' && (
                                    <span className="font-mono text-[10px] text-amber-700 dark:text-amber-400 bg-amber-50 dark:bg-amber-950/40 border border-amber-200 dark:border-amber-800/60 px-1.5 py-0.5 rounded font-medium">
                                      number
                                    </span>
                                  )}
                                  {category === 'list' && (
                                    <span className="font-mono text-[10px] text-blue-700 dark:text-blue-400 bg-blue-50 dark:bg-blue-950/40 border border-blue-200 dark:border-blue-800/60 px-1.5 py-0.5 rounded font-medium">
                                      {isComplexList ? 'list<object>' : 'list [ ]'}
                                    </span>
                                  )}
                                  {category === 'json' && (
                                    <span className="font-mono text-[10px] text-purple-700 dark:text-purple-400 bg-purple-50 dark:bg-purple-950/40 border border-purple-200 dark:border-purple-800/60 px-1.5 py-0.5 rounded font-medium">
                                      json &#123; &#125;
                                    </span>
                                  )}
                                  {category === 'string' && (
                                    <span className="font-mono text-[10px] text-slate-600 dark:text-slate-400 bg-slate-100 dark:bg-slate-800 border border-slate-200 dark:border-slate-750 px-1.5 py-0.5 rounded font-medium">
                                      string
                                    </span>
                                  )}
                                </div>

                                {/* 控件渲染 */}
                                {category === 'boolean' ? (
                                  <div className="flex items-center gap-2 pt-0.5">
                                    <button
                                      type="button"
                                      onClick={() => handleFieldChange(field.name, true)}
                                      className={`px-3 py-1 text-xs font-semibold rounded-md border transition-all ${
                                        formData[field.name] === true
                                          ? 'bg-emerald-600 text-white border-emerald-600 shadow-2xs'
                                          : 'bg-white dark:bg-slate-800 text-slate-600 dark:text-slate-300 border-slate-200 dark:border-slate-700 hover:bg-slate-50 dark:hover:bg-slate-750'
                                      }`}
                                    >
                                      ✓ True (是)
                                    </button>
                                    <button
                                      type="button"
                                      onClick={() => handleFieldChange(field.name, false)}
                                      className={`px-3 py-1 text-xs font-semibold rounded-md border transition-all ${
                                        formData[field.name] === false
                                          ? 'bg-rose-600 text-white border-rose-600 shadow-2xs'
                                          : 'bg-white dark:bg-slate-800 text-slate-600 dark:text-slate-300 border-slate-200 dark:border-slate-700 hover:bg-slate-50 dark:hover:bg-slate-750'
                                      }`}
                                    >
                                      ✕ False (否)
                                    </button>
                                    {!field.required && (
                                      <button
                                        type="button"
                                        onClick={() => handleFieldChange(field.name, undefined)}
                                        className={`px-2.5 py-1 text-xs rounded-md border transition-all ${
                                          formData[field.name] === undefined ||
                                          formData[field.name] === null
                                            ? 'bg-slate-100 dark:bg-slate-800 text-slate-700 dark:text-slate-300 border-slate-300 dark:border-slate-700 font-medium'
                                            : 'bg-white dark:bg-slate-800 text-slate-400 dark:text-slate-500 border-slate-200 dark:border-slate-700 hover:bg-slate-50 dark:hover:bg-slate-750'
                                        }`}
                                      >
                                        未设置
                                      </button>
                                    )}
                                  </div>
                                ) : category === 'number' ? (
                                  <Input
                                    type="number"
                                    value={
                                      formData[field.name] === undefined ||
                                      formData[field.name] === null
                                        ? ''
                                        : String(formData[field.name])
                                    }
                                    onChange={(e) => {
                                      const raw = e.target.value
                                      if (raw === '') {
                                        handleFieldChange(field.name, undefined)
                                      } else {
                                        const num = Number(raw)
                                        handleFieldChange(field.name, isNaN(num) ? raw : num)
                                      }
                                    }}
                                    placeholder={
                                      field.placeholder ||
                                      `请输入数值 (如: ${field.default ?? 0})`
                                    }
                                    className="h-8 text-xs bg-white dark:bg-slate-850 border-slate-200 dark:border-slate-750 text-slate-800 dark:text-slate-100 focus-visible:ring-blue-500 font-mono"
                                  />
                                ) : category === 'list' ? (
                                  <DynamicListForm
                                    value={formData[field.name]}
                                    placeholder={field.placeholder}
                                    field={field}
                                    onChange={(items) => handleFieldChange(field.name, items)}
                                  />
                                ) : category === 'json' ? (
                                  <StructuredObjectForm
                                    field={field}
                                    value={formData[field.name]}
                                    placeholder={field.placeholder}
                                    onChange={(obj) => handleFieldChange(field.name, obj)}
                                  />
                                ) : field.name === 'Text' ||
                                  field.name === 'Introduction' ||
                                  field.name === 'Notification' ||
                                  field.name === 'SelfSignature' ? (
                                  <textarea
                                    value={
                                      formData[field.name] !== undefined
                                        ? String(formData[field.name])
                                        : ''
                                    }
                                    onChange={(e) => handleFieldChange(field.name, e.target.value)}
                                    rows={2}
                                    placeholder={
                                      field.placeholder || `请输入 ${field.label || field.name}`
                                    }
                                    className="w-full rounded-lg border border-slate-200 dark:border-slate-750 bg-white dark:bg-slate-850 p-2 text-xs text-slate-800 dark:text-slate-100 focus:outline-none focus:ring-1 focus:ring-blue-500"
                                  />
                                ) : (
                                  <Input
                                    value={
                                      formData[field.name] !== undefined
                                        ? typeof formData[field.name] === 'object'
                                          ? JSON.stringify(formData[field.name])
                                          : String(formData[field.name])
                                        : ''
                                    }
                                    onChange={(e) => handleFieldChange(field.name, e.target.value)}
                                    placeholder={
                                      field.placeholder || `请输入 ${field.label || field.name}`
                                    }
                                    className="h-8 text-xs bg-white dark:bg-slate-850 border-slate-200 dark:border-slate-750 text-slate-800 dark:text-slate-100 focus-visible:ring-blue-500"
                                  />
                                )}

                                {field.help_text && (
                                  <p className="text-[10px] text-slate-400 dark:text-slate-500">{field.help_text}</p>
                                )}
                              </div>
                            )
                          })
                        ) : (
                          <div className="col-span-2 text-xs text-slate-400 dark:text-slate-500 italic py-2">
                            当前接口无需独立表单参数，可切换至「JSON (Raw)」模式微调 Payload。
                          </div>
                        )}
                      </div>
                    ) : (
                      <JsonCodeEditor
                        value={jsonText}
                        onChange={handleJsonChange}
                        error={jsonError}
                        height="260px"
                        minHeight="180px"
                        placeholder="请输入请求 JSON 报文..."
                        label="请求包体 (JSON RAW)"
                        showLineNumbers={true}
                      />
                    )}
                  </div>
                )}

                {/* 3. Headers Tab */}
                {requestTab === 'headers' && (
                  <div className="space-y-3">
                    <div className="flex items-center justify-between">
                      <p className="text-[11px] text-slate-500 dark:text-slate-400">
                        {isApi3
                          ? '依据官方文档 (20459)，API 3.0 v3 签名的公共参数统一放置在 HTTP Headers 头部中，请求时由 TC3 签名引擎自动组装：'
                          : '腾讯云 IM 服务端通信标准协议请求头：'}
                      </p>
                      {isApi3 && (
                        <a
                          href="https://cloud.tencent.com/document/api/267/20459"
                          target="_blank"
                          rel="noreferrer"
                          className="text-[11px] text-blue-600 dark:text-blue-400 hover:underline flex items-center gap-1 font-medium"
                        >
                          <span>官方公共参数文档 (20459)</span>
                          <ExternalLink className="h-3 w-3" />
                        </a>
                      )}
                    </div>
                    <div className="rounded-xl border border-slate-200 dark:border-slate-800 overflow-x-auto bg-white dark:bg-slate-900 shadow-2xs">
                      <table className="w-full text-left text-xs">
                        <thead className="bg-slate-50 dark:bg-slate-850 border-b border-slate-200 dark:border-slate-800 text-slate-500 dark:text-slate-400 font-semibold">
                          <tr>
                            <th className="py-2 px-3 w-12 text-center">状态</th>
                            <th className="py-2 px-3 w-44">Header 名 (Key)</th>
                            <th className="py-2 px-3">Header 值 (Value)</th>
                            <th className="py-2 px-3">说明 (Description)</th>
                          </tr>
                        </thead>
                        <tbody className="divide-y divide-slate-100 dark:divide-slate-800 font-mono text-[11px]">
                          {effectiveHeaders.map((h) => (
                            <tr key={h.key} className="hover:bg-slate-50/60 dark:hover:bg-slate-850/60">
                              <td className="py-2 px-3 text-center">
                                <input
                                  type="checkbox"
                                  checked={true}
                                  readOnly
                                  className={`rounded border-slate-300 dark:border-slate-600 focus:ring-0 cursor-not-allowed opacity-75 ${
                                    isApi3 ? 'text-emerald-500' : 'text-orange-500'
                                  }`}
                                />
                              </td>
                              <td className="py-2 px-3 font-semibold text-slate-800 dark:text-slate-200">
                                {h.key}
                                {h.required && <span className="text-rose-500 ml-1 font-bold">*</span>}
                              </td>
                              <td className="py-2 px-3 text-blue-600 dark:text-blue-400 truncate max-w-xs font-medium">
                                {h.value}
                              </td>
                              <td className="py-2 px-3 text-slate-500 dark:text-slate-400 font-sans text-xs">
                                {h.desc}
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </div>
                )}

                {/* 4. Docs Tab */}
                {requestTab === 'docs' && (
                  <ApiDocsViewer
                    api={activeApi}
                    onApplyExample={handleApplyDocExample}
                    onToggleExpand={() => setIsDocsModalOpen(true)}
                  />
                )}
              </div>
            </div>
          </Panel>

          {!isResponseCollapsed && (
            <>
              {/* Resizable 分隔手柄 */}
              <ResizeHandle
                direction="vertical"
                disableDoubleClick={true}
                onDoubleClick={handleToggleResponseHeight}
                title="双击在展开/还原响应面板之间切换，也可上下拖拽调整高度"
              />

              {/* 下窗格：Response Inspector (Apifox 响应检查面板) */}
              <Panel id="response-panel" defaultSize={48} minSize={20}>
                <div className="flex h-full flex-col bg-slate-50/50 dark:bg-slate-950 text-slate-800 dark:text-slate-100 overflow-hidden">
                  {/* Response Status Header */}
                  <div
                    onDoubleClick={handleToggleResponseHeight}
                    title="双击在展开/还原响应面板之间切换"
                    className="flex flex-wrap items-center justify-between border-t border-b border-slate-200 dark:border-slate-800 bg-white/95 dark:bg-slate-900/90 px-3 sm:px-4 py-2 select-none shadow-2xs cursor-pointer gap-2"
                  >
                    <div className="flex flex-wrap items-center gap-1.5 sm:gap-2 min-w-0">
                      <span className="text-xs font-bold text-slate-800 dark:text-slate-200 flex items-center gap-1.5 shrink-0">
                        <span>Response</span>
                      </span>

                      {invokeResponse && (
                        <div className="flex items-center gap-2">
                          {/* 预览模式徽章 */}
                          {invokeResponse.dry_run ? (
                            <Badge className="font-mono text-[10px] px-2 py-0.5 bg-violet-50 text-violet-700 border border-violet-200 dark:bg-violet-600/30 dark:text-violet-300 dark:border-violet-500/50">
                              <Sparkles className="h-3 w-3 mr-1 text-violet-600 dark:text-violet-400" />
                              PREVIEW (DRY-RUN)
                            </Badge>
                          ) : (
                            <Badge
                              variant="outline"
                              className={`font-mono text-[10px] px-2 py-0.5 font-bold shadow-2xs ${
                                (invokeResponse.response?.Response && !invokeResponse.response?.Response?.Error) ||
                                (invokeResponse.response?.ActionStatus === 'OK' && invokeResponse.response?.ErrorCode === 0)
                                  ? 'border-emerald-200 bg-emerald-50 text-emerald-700 dark:border-emerald-800/60 dark:bg-emerald-950/40 dark:text-emerald-400'
                                  : 'border-rose-300 bg-rose-50 text-rose-700 dark:border-rose-900/60 dark:bg-rose-950/50 dark:text-rose-300'
                              }`}
                            >
                              {(invokeResponse.response?.Response && !invokeResponse.response?.Response?.Error) ||
                              (invokeResponse.response?.ActionStatus === 'OK' && invokeResponse.response?.ErrorCode === 0) ? (
                                <span className="flex items-center gap-1">
                                  <CheckCircle2 className="h-3 w-3 text-emerald-600 dark:text-emerald-400" />
                                  200 OK
                                </span>
                              ) : (
                                <span className="flex items-center gap-1">
                                  <AlertTriangle className="h-3 w-3 text-rose-600 dark:text-rose-400 shrink-0" />
                                  FAIL ({invokeResponse.response?.Response?.Error?.Code || (invokeResponse.response?.ErrorCode !== undefined ? `Code: ${invokeResponse.response?.ErrorCode}` : `HTTP ${invokeResponse.status_code || 'ERR'}`)})
                                </span>
                              )}
                            </Badge>
                          )}

                          {invokeResponse.cost_ms !== undefined && (
                            <span className="font-mono text-[11px] text-slate-600 dark:text-slate-400 flex items-center gap-1 bg-slate-100 dark:bg-slate-850 px-2 py-0.5 rounded border border-slate-200 dark:border-slate-750">
                              <Clock className="h-3 w-3 text-amber-500 dark:text-amber-400" />
                              {invokeResponse.cost_ms} ms
                            </span>
                          )}

                          <span className="font-mono text-[11px] text-slate-600 dark:text-slate-400 bg-slate-100 dark:bg-slate-850 px-2 py-0.5 rounded border border-slate-200 dark:border-slate-750">
                            {responseSize}
                          </span>
                        </div>
                      )}

                      {/* Postman 风格的 Response 调用历史快速回溯 */}
                      {currentTab?.invokeHistory && currentTab.invokeHistory.length > 0 && (
                        <div className="relative" ref={responseHistoryDropdownRef}>
                          <button
                            type="button"
                            onClick={(e) => {
                              e.stopPropagation()
                              setIsResponseHistoryOpen(!isResponseHistoryOpen)
                            }}
                            className="flex items-center gap-1 text-[11px] font-mono px-2 py-0.5 rounded-lg bg-slate-100 hover:bg-slate-200 dark:bg-slate-800 dark:hover:bg-slate-750 text-slate-700 dark:text-slate-200 border border-slate-200 dark:border-slate-700 transition-colors cursor-pointer select-none shadow-2xs"
                            title="查看当前接口的历史调试响应与回执记录 (Postman风格)"
                          >
                            <History className="h-3 w-3 text-blue-500" />
                            <span>历史响应 ({currentTab.invokeHistory.length})</span>
                            <ChevronDown className="h-3 w-3 opacity-60" />
                          </button>

                          {isResponseHistoryOpen && (
                            <div
                              onClick={(e) => e.stopPropagation()}
                              className="absolute left-0 top-full mt-1.5 w-84 rounded-xl border border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900 shadow-2xl z-50 p-2 space-y-1.5 animate-in fade-in-50 zoom-in-95 duration-100"
                            >
                              <div className="flex items-center justify-between px-1 pb-1.5 text-[10px] font-bold text-slate-400 border-b border-slate-100 dark:border-slate-800">
                                <span>历史调用响应 (最多保留 15 次)</span>
                                <button
                                  type="button"
                                  onClick={() => {
                                    if (currentTab) clearTabInvokeHistory(currentTab.id)
                                    setIsResponseHistoryOpen(false)
                                  }}
                                  className="text-rose-500 hover:text-rose-600 dark:hover:text-rose-400 cursor-pointer"
                                >
                                  清空历史
                                </button>
                              </div>

                              <div className="max-h-64 overflow-y-auto space-y-1 pr-0.5">
                                {currentTab.invokeHistory.map((rec, idx) => {
                                  const isSelected = invokeResponse === rec.response
                                  return (
                                    <div
                                      key={rec.id}
                                      onClick={() => {
                                        if (currentTab) restoreTabInvokeHistory(currentTab.id, rec, false)
                                        setIsResponseHistoryOpen(false)
                                      }}
                                      className={`flex items-center justify-between p-2 rounded-lg text-xs transition-colors cursor-pointer border ${
                                        isSelected
                                          ? 'bg-blue-50/80 border-blue-200 dark:bg-blue-950/40 dark:border-blue-800'
                                          : 'border-transparent hover:bg-slate-50 dark:hover:bg-slate-800/80'
                                      }`}
                                    >
                                      <div className="flex items-center gap-2 min-w-0">
                                        <span
                                          className={`w-1.5 h-1.5 rounded-full shrink-0 ${
                                            rec.isOk ? 'bg-emerald-500' : 'bg-rose-500'
                                          }`}
                                        />
                                        <span className="font-mono text-[11px] text-slate-500 dark:text-slate-400 shrink-0">
                                          {rec.timeStr}
                                        </span>
                                        <Badge
                                          variant="outline"
                                          className={`font-mono text-[9px] px-1.5 py-0 shrink-0 font-semibold ${
                                            rec.isOk
                                              ? 'border-emerald-200 bg-emerald-50 text-emerald-700 dark:border-emerald-800/60 dark:bg-emerald-950/40 dark:text-emerald-400'
                                              : 'border-rose-300 bg-rose-50 text-rose-700 dark:border-rose-900/60 dark:bg-rose-950/50 dark:text-rose-300'
                                          }`}
                                        >
                                          {rec.dryRun
                                            ? 'PREVIEW'
                                            : rec.isOk
                                            ? '200 OK'
                                            : `ERR ${rec.errorCode}`}
                                        </Badge>
                                      </div>

                                      <div className="flex items-center gap-1.5 shrink-0">
                                        <span className="font-mono text-[10px] text-slate-400">
                                          {rec.costMs}ms
                                        </span>
                                        <button
                                          type="button"
                                          onClick={(e) => {
                                            e.stopPropagation()
                                            if (currentTab) restoreTabInvokeHistory(currentTab.id, rec, true)
                                            setIsResponseHistoryOpen(false)
                                            showToast({
                                              type: 'success',
                                              message: `已恢复该次请求的入参并切换回执 [${rec.timeStr}]`,
                                            })
                                          }}
                                          className="text-[10px] text-blue-600 dark:text-blue-400 hover:underline px-1 py-0.5 rounded hover:bg-blue-50 dark:hover:bg-blue-950/40 cursor-pointer font-medium"
                                          title="同时还原该次请求传入的参数 (Payload)"
                                        >
                                          还原入参
                                        </button>
                                      </div>
                                    </div>
                                  )
                                })}
                              </div>
                            </div>
                          )}
                        </div>
                      )}
                    </div>

                    {/* Response Sub-views & Copy Actions */}
                    <div className="flex items-center gap-1.5 sm:gap-2 shrink-0 overflow-x-auto w-full sm:w-auto justify-between sm:justify-end">
                      <div className="flex items-center rounded-lg bg-slate-100 dark:bg-slate-850 p-0.5 text-xs font-medium border border-slate-200 dark:border-slate-750">
                        <button
                          onClick={() => handleSetResponseTab('pretty')}
                          className={`px-2 py-0.5 rounded text-[11px] font-semibold transition-all cursor-pointer ${
                            responseTab === 'pretty'
                              ? 'bg-white dark:bg-slate-750 text-slate-900 dark:text-white shadow-2xs'
                              : 'text-slate-600 dark:text-slate-400 hover:text-slate-900 dark:hover:text-slate-200'
                          }`}
                        >
                          Pretty
                        </button>
                        <button
                          onClick={() => handleSetResponseTab('sent')}
                          className={`px-2 py-0.5 rounded text-[11px] font-semibold transition-all cursor-pointer ${
                            responseTab === 'sent'
                              ? 'bg-white dark:bg-slate-750 text-slate-900 dark:text-white shadow-2xs'
                              : 'text-slate-600 dark:text-slate-400 hover:text-slate-900 dark:hover:text-slate-200'
                          }`}
                        >
                          Sent Payload
                        </button>
                        <button
                          onClick={() => handleSetResponseTab('curl')}
                          className={`px-2 py-0.5 rounded text-[11px] font-semibold transition-all cursor-pointer ${
                            responseTab === 'curl'
                              ? 'bg-white dark:bg-slate-750 text-slate-900 dark:text-white shadow-2xs'
                              : 'text-slate-600 dark:text-slate-400 hover:text-slate-900 dark:hover:text-slate-200'
                          }`}
                        >
                          cURL
                        </button>
                        <button
                          onClick={() => handleSetResponseTab('raw')}
                          className={`px-2 py-0.5 rounded text-[11px] font-semibold transition-all cursor-pointer ${
                            responseTab === 'raw'
                              ? 'bg-white dark:bg-slate-750 text-slate-900 dark:text-white shadow-2xs'
                              : 'text-slate-600 dark:text-slate-400 hover:text-slate-900 dark:hover:text-slate-200'
                          }`}
                        >
                          Raw
                        </button>
                      </div>

                      {invokeResponse && (
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={() =>
                            handleCopy(
                              JSON.stringify(invokeResponse.response || invokeResponse, null, 2)
                            )
                          }
                          className="h-7 text-xs text-slate-600 hover:text-slate-900 dark:text-slate-300 dark:hover:text-white hover:bg-slate-200/60 dark:hover:bg-slate-800 gap-1 cursor-pointer"
                        >
                          {copied ? (
                            <Check className="h-3 w-3 text-emerald-600 dark:text-emerald-400" />
                          ) : (
                            <Copy className="h-3 w-3" />
                          )}
                          <span>{copied ? '已复制' : '复制'}</span>
                        </Button>
                      )}

                      {/* 收起 Response 按钮 */}
                      <button
                        onClick={() => setIsResponseCollapsed(true)}
                        className="flex items-center gap-1 px-2 py-1 rounded text-[11px] text-slate-500 hover:text-slate-900 dark:text-slate-400 dark:hover:text-white hover:bg-slate-200/60 dark:hover:bg-slate-800 transition-colors cursor-pointer border border-transparent hover:border-slate-300 dark:hover:border-slate-700 ml-1"
                        title="收起 Response 面板至底部"
                      >
                        <ChevronDown className="h-3.5 w-3.5" />
                        <span>收起</span>
                      </button>
                    </div>
                  </div>

              {/* Response Body Area */}
              <div className="flex-1 min-h-0 flex flex-col p-3 sm:p-4 font-mono text-xs overflow-hidden">
                {invokeResponse || responseTab === 'curl' ? (
                  <div className="flex-1 min-h-0 flex flex-col h-full">
                    {/* 预览模式专有详细说明条 */}
                    {invokeResponse?.dry_run && (
                      <div className="mb-3 shrink-0 rounded-xl bg-violet-50 dark:bg-violet-950/70 border border-violet-200 dark:border-violet-800/80 p-3 text-xs text-violet-800 dark:text-violet-200 font-sans shadow-2xs">
                        <div className="flex items-center gap-2 font-bold text-violet-900 dark:text-violet-300 mb-1">
                          <Sparkles className="h-4 w-4 text-violet-600 dark:text-violet-400" />
                          <span>【预览演练成功】已完成入参校验与结构组装，未发送网络请求</span>
                        </div>
                        <p className="text-[11px] text-violet-700/90 dark:text-violet-300/90 leading-relaxed">
                          当前处于预览模式。已成功验证表单/JSON参数并生成完整的发送报文。您可在上方或下方「Sent Payload」与「cURL」中复核报文，确认无误后可关闭右上角「预览模式」发起真实网络调用。
                        </p>
                      </div>
                    )}

                    {/* 腾讯云 API 3.0 官方错误码智能诊断与排查指引 (基于 Doc 30427) */}
                    {invokeResponse?.response?.Response?.Error && (
                      <div className="mb-3 shrink-0 rounded-xl bg-rose-50/90 dark:bg-rose-950/60 border border-rose-200 dark:border-rose-900/70 p-3.5 text-xs text-rose-900 dark:text-rose-200 font-sans shadow-2xs space-y-1.5">
                        <div className="flex items-center justify-between">
                          <div className="flex items-center gap-2 font-bold text-rose-900 dark:text-rose-200">
                            <AlertTriangle className="h-4 w-4 text-rose-600 dark:text-rose-400 shrink-0" />
                            <span>【官方错误码诊断】{invokeResponse.response.Response.Error.Code}</span>
                          </div>
                          <Badge variant="outline" className="font-mono text-[9px] bg-rose-100 dark:bg-rose-900/80 text-rose-700 dark:text-rose-300 border-rose-300 dark:border-rose-800">
                            Doc ID 30427 官方规范
                          </Badge>
                        </div>
                        {invokeResponse.response.Response.Error.Message && (
                          <div className="text-[11px] font-mono text-rose-800 dark:text-rose-300 bg-white/70 dark:bg-slate-900/70 p-2 rounded border border-rose-200/50 dark:border-rose-900/40">
                            {invokeResponse.response.Response.Error.Message}
                          </div>
                        )}
                        <div className="flex items-start gap-1.5 text-[11px] text-rose-800 dark:text-rose-200 pt-0.5">
                          <Sparkles className="h-3.5 w-3.5 text-amber-500 shrink-0 mt-0.5" />
                          <span>
                            建议排查：请检查当前入参（流名、域名配置、模板 ID 或时间戳）是否准确，并确认账号具有该接口权限。可在【文档中心 &gt; 公共规范中心】直接检索该错误码官方排查指引。
                          </span>
                        </div>
                      </div>
                    )}

                    {responseTab === 'pretty' && invokeResponse && (
                      <div className="flex-1 min-h-0 flex flex-col h-full">
                        <JsonCodeEditor
                          value={JSON.stringify(invokeResponse.response || invokeResponse, null, 2)}
                          readOnly={true}
                          showLineNumbers={true}
                          className="flex-1 h-full min-h-[160px]"
                          label="格式化应答 (Response Pretty)"
                        />
                      </div>
                    )}

                    {responseTab === 'sent' && invokeResponse && (
                      <div className="flex-1 min-h-0 flex flex-col space-y-2 h-full">
                        <p className="text-[11px] text-slate-500 dark:text-slate-400 font-sans shrink-0">
                          {invokeResponse.dry_run ? '预览模式构建出的标准发送报文包体：' : '实际发送至网关的报文数据包：'}
                        </p>
                        <div className="flex-1 min-h-0 flex flex-col h-full">
                          <JsonCodeEditor
                            value={JSON.stringify(invokeResponse.sent_payload || {}, null, 2)}
                            readOnly={true}
                            showLineNumbers={true}
                            className="flex-1 h-full min-h-[160px]"
                            label="发送报文 Payload"
                          />
                        </div>
                      </div>
                    )}

                    {responseTab === 'curl' && (
                      <div className="flex-1 min-h-0 flex flex-col space-y-2 h-full">
                        <div className="flex items-center justify-between font-sans text-[11px] text-slate-500 dark:text-slate-400 shrink-0 flex-wrap gap-2">
                          <div className="flex items-center gap-1.5">
                            <span>可在 Linux/macOS 终端中直接脱机执行：</span>
                            {hasRealCredentials ? (
                              <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-medium bg-amber-50 text-amber-700 dark:bg-amber-950/40 dark:text-amber-400 border border-amber-200/60 dark:border-amber-800/60">
                                <ShieldAlert className="h-3 w-3 text-amber-600 dark:text-amber-400" />
                                包含实时有效管理员凭据
                              </span>
                            ) : invokeResponse?.credentials_redacted ? (
                              <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-medium bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-400 border border-slate-200 dark:border-slate-700">
                                <ShieldCheck className="h-3 w-3 text-emerald-600 dark:text-emerald-400" />
                                企业权限脱敏保护
                              </span>
                            ) : (
                              <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-medium bg-blue-50 text-blue-600 dark:bg-blue-950/40 dark:text-blue-400 border border-blue-200/60 dark:border-blue-800/60">
                                模板（点击预览执行可注入实时签名）
                              </span>
                            )}
                          </div>

                          <div className="flex items-center gap-1.5">
                            {hasRealCredentials && (
                              <button
                                type="button"
                                onClick={() => setShowPlainCurl(!showPlainCurl)}
                                className="flex items-center gap-1 px-2 py-0.5 rounded text-[11px] text-slate-600 dark:text-slate-400 hover:text-slate-900 dark:hover:text-slate-200 hover:bg-slate-100 dark:hover:bg-slate-800 border border-slate-200 dark:border-slate-750 transition-colors cursor-pointer"
                                title={showPlainCurl ? '隐藏屏幕上的明文签名凭据' : '在屏幕上显示完整明文签名凭据'}
                              >
                                {showPlainCurl ? (
                                  <>
                                    <EyeOff className="h-3 w-3 text-slate-500" />
                                    <span>隐藏明文凭据</span>
                                  </>
                                ) : (
                                  <>
                                    <Eye className="h-3 w-3 text-slate-500" />
                                    <span>显示明文凭据</span>
                                  </>
                                )}
                              </button>
                            )}

                            {hasRealCredentials && (
                              <button
                                type="button"
                                onClick={handleCopyMaskedCurl}
                                className="px-2 py-0.5 rounded text-[11px] text-slate-600 dark:text-slate-400 hover:text-slate-900 dark:hover:text-slate-200 hover:bg-slate-100 dark:hover:bg-slate-800 border border-slate-200 dark:border-slate-750 transition-colors cursor-pointer"
                                title="复制脱敏后的命令模板，适合提交工单、代码审查或公开分享"
                              >
                                复制脱敏模板
                              </button>
                            )}

                            <Button
                              size="sm"
                              variant="outline"
                              onClick={handleCopyExecutableCurl}
                              className="h-6 px-2.5 text-[11px] font-medium text-orange-600 dark:text-orange-400 hover:text-orange-700 dark:hover:text-orange-300 border-orange-200 dark:border-orange-900/60 hover:bg-orange-50 dark:hover:bg-orange-950/40 gap-1 cursor-pointer"
                              title="复制含有效签名的完整可执行命令，可在终端中直接粘贴脱机运行"
                            >
                              {copiedCurl ? (
                                <>
                                  <Check className="h-3 w-3 text-emerald-600 dark:text-emerald-400" />
                                  <span>已复制真实命令</span>
                                </>
                              ) : (
                                <>
                                  <Terminal className="h-3 w-3" />
                                  <span>{hasRealCredentials ? '复制可执行真实命令' : '复制命令'}</span>
                                </>
                              )}
                            </Button>
                          </div>
                        </div>

                        <div className="relative flex-1 min-h-0 flex flex-col">
                          <pre className="flex-1 min-h-0 text-amber-800 dark:text-amber-300 leading-relaxed overflow-auto whitespace-pre bg-white dark:bg-slate-900/60 p-4 rounded-xl border border-slate-200/80 dark:border-slate-800 shadow-2xs font-mono text-xs select-all">
                            {showPlainCurl ? effectiveCurlCommand : maskedCurlCommand}
                          </pre>
                          {!showPlainCurl && hasRealCredentials && (
                            <div className="absolute bottom-2 right-3 pointer-events-none text-[10px] text-slate-400 dark:text-slate-500 bg-white/80 dark:bg-slate-900/80 px-2 py-0.5 rounded backdrop-blur border border-slate-200/50 dark:border-slate-800/50">
                              🔒 屏幕已脱敏保护（复制时包含真实有效凭据）
                            </div>
                          )}
                        </div>
                      </div>
                    )}

                    {responseTab === 'raw' && invokeResponse && (
                      <div className="flex-1 min-h-0 flex flex-col h-full">
                        <JsonCodeEditor
                          value={JSON.stringify(invokeResponse.response || invokeResponse)}
                          readOnly={true}
                          showLineNumbers={false}
                          className="flex-1 h-full min-h-[160px]"
                          label="原生应答 (RAW)"
                        />
                      </div>
                    )}
                  </div>
                ) : (
                  <div className="flex flex-col items-center justify-center h-full min-h-[160px] text-center text-slate-400 dark:text-slate-500">
                    <Send className="h-8 w-8 text-slate-300 dark:text-slate-700 stroke-1 mb-2 animate-pulse" />
                    <p className="text-xs font-semibold text-slate-600 dark:text-slate-400">
                      {isPreviewMode
                        ? '点击「预览执行」或按 ⌘ + Enter 演练校验（不发网络请求）'
                        : '点击「发送」或按 ⌘ + Enter 获取响应结果'}
                    </p>
                    <p className="text-[11px] text-slate-400 dark:text-slate-600 mt-1">
                      {isPreviewMode
                        ? '预览模式下将执行字段格式验证与完整报文组装，安全无线上副作用'
                        : '支持实时耗时统计 (ms)、状态码判定、数据包尺寸与 cURL 导出'}
                    </p>
                  </div>
                )}
              </div>
            </div>
          </Panel>
            </>
          )}
        </Group>

        {/* 当 Response 面板收起时展示底部状态栏 */}
        {isResponseCollapsed && (
          <div
            onClick={() => setIsResponseCollapsed(false)}
            className="h-10 px-4 bg-slate-100 hover:bg-slate-200/80 dark:bg-slate-900 dark:hover:bg-slate-850 border-t-2 border-blue-500 flex items-center justify-between cursor-pointer select-none transition-colors shrink-0 z-20 shadow-sm"
            title="点击展开 Response 响应面板"
          >
            <div className="flex items-center gap-3">
              <div className="flex items-center gap-1.5 text-xs font-bold text-slate-800 dark:text-slate-200">
                <ChevronUp className="h-4 w-4 text-blue-500 animate-bounce" />
                <span>Response 响应面板</span>
                <span className="text-[10px] text-slate-500 dark:text-slate-400 font-normal">(已收起，点击展开)</span>
              </div>

              {invokeResponse && (
                <div className="flex items-center gap-2">
                  {invokeResponse.dry_run ? (
                    <Badge className="font-mono text-[9px] px-1.5 py-0 bg-violet-50 text-violet-700 border border-violet-200 dark:bg-violet-600/30 dark:text-violet-300 dark:border-violet-500/50">
                      PREVIEW
                    </Badge>
                  ) : invokeResponse.response?.ActionStatus === 'OK' && invokeResponse.response?.ErrorCode === 0 ? (
                    <span className="text-[11px] font-mono font-bold text-emerald-600 dark:text-emerald-400 flex items-center gap-1">
                      <CheckCircle2 className="h-3 w-3" />
                      200 OK
                    </span>
                  ) : (
                    <span className="text-[11px] font-mono font-bold text-rose-600 dark:text-rose-400 flex items-center gap-1">
                      <AlertTriangle className="h-3 w-3" />
                      FAIL ({invokeResponse.response?.ErrorCode ?? 'ERR'})
                    </span>
                  )}

                  {invokeResponse.cost_ms !== undefined && (
                    <span className="font-mono text-[10px] text-slate-600 dark:text-slate-400 bg-white dark:bg-slate-850 px-1.5 py-0.2 rounded border border-slate-300 dark:border-slate-750">
                      {invokeResponse.cost_ms} ms
                    </span>
                  )}

                  <span className="font-mono text-[10px] text-slate-600 dark:text-slate-400 bg-white dark:bg-slate-850 px-1.5 py-0.2 rounded border border-slate-300 dark:border-slate-750">
                    {responseSize}
                  </span>
                </div>
              )}
            </div>

            <div className="flex items-center gap-1.5 text-xs text-blue-600 hover:text-blue-700 dark:text-blue-400 dark:hover:text-blue-300 font-semibold">
              <span>展开面板</span>
              <Maximize2 className="h-3.5 w-3.5" />
            </div>
          </div>
        )}
      </div>

      {/* 标签页右键菜单 (Context Menu) */}
      <AnimatePresence>
        {contextMenu.visible && contextMenu.tab && (
          <motion.div
            initial={{ opacity: 0, scale: 0.95 }}
            animate={{ opacity: 1, scale: 1 }}
            exit={{ opacity: 0, scale: 0.95 }}
            transition={{ duration: 0.08 }}
            style={{ top: contextMenu.y, left: contextMenu.x }}
            className="fixed z-50 w-52 rounded-xl border border-slate-200/90 dark:border-slate-800 bg-white/95 dark:bg-slate-900/95 p-1 text-xs shadow-2xl backdrop-blur-md select-none"
            onMouseDown={(e) => e.stopPropagation()}
          >
            <div className="px-2.5 py-1 text-[10px] font-bold text-slate-400 dark:text-slate-500 border-b border-slate-100 dark:border-slate-800 mb-0.5 truncate">
              {getCleanApiName(contextMenu.tab.api.name, contextMenu.tab.api.command, contextMenu.tab.api.action)}
            </div>

            <button
              type="button"
              onClick={() => {
                closeTab(contextMenu.tab!.id)
                setContextMenu((prev) => ({ ...prev, visible: false }))
              }}
              className="flex w-full items-center justify-between px-2.5 py-1.5 rounded-lg text-slate-700 dark:text-slate-300 hover:bg-slate-100 dark:hover:bg-slate-800 hover:text-slate-900 dark:hover:text-white cursor-pointer"
            >
              <span>关闭标签页</span>
              <span className="font-mono text-[10px] text-slate-400 dark:text-slate-500">⌘W</span>
            </button>

            <button
              type="button"
              onClick={() => {
                closeTabsToRight(contextMenu.tab!.id)
                setContextMenu((prev) => ({ ...prev, visible: false }))
              }}
              className="flex w-full items-center justify-between px-2.5 py-1.5 rounded-lg text-slate-700 dark:text-slate-300 hover:bg-slate-100 dark:hover:bg-slate-800 hover:text-slate-900 dark:hover:text-white cursor-pointer"
            >
              <span>关闭右侧标签页</span>
            </button>

            <button
              type="button"
              onClick={() => {
                closeOtherTabs(contextMenu.tab!.id)
                setContextMenu((prev) => ({ ...prev, visible: false }))
              }}
              className="flex w-full items-center justify-between px-2.5 py-1.5 rounded-lg text-slate-700 dark:text-slate-300 hover:bg-slate-100 dark:hover:bg-slate-800 hover:text-slate-900 dark:hover:text-white cursor-pointer"
            >
              <span>关闭其他标签页</span>
            </button>

            <button
              type="button"
              onClick={() => {
                closeAllTabs()
                setContextMenu((prev) => ({ ...prev, visible: false }))
              }}
              className="flex w-full items-center justify-between px-2.5 py-1.5 rounded-lg text-rose-600 dark:text-rose-400 hover:bg-rose-50 dark:hover:bg-rose-950/40 cursor-pointer"
            >
              <span>关闭全部标签页</span>
            </button>

            <div className="my-1 border-t border-slate-100 dark:border-slate-800" />

            <button
              type="button"
              onClick={() => {
                setApiId(contextMenu.tab!.api.api_id)
                showToast({
                  type: 'success',
                  message: `已将「${getCleanApiName(contextMenu.tab!.api.name, contextMenu.tab!.api.command, contextMenu.tab!.api.action)}」设为批处理目标接口`,
                  actionLabel: '前往批处理中心 →',
                  onAction: onSwitchToBatch,
                })
                setContextMenu((prev) => ({ ...prev, visible: false }))
              }}
              className="flex w-full items-center gap-2 px-2.5 py-1.5 rounded-lg text-slate-700 dark:text-slate-300 hover:bg-slate-100 dark:hover:bg-slate-800 hover:text-slate-900 dark:hover:text-white cursor-pointer"
            >
              <Layers className="h-3.5 w-3.5 text-slate-400 dark:text-slate-500" />
              <span>设为批处理目标</span>
            </button>

            <button
              type="button"
              onClick={() => {
                navigator.clipboard.writeText(contextMenu.tab!.api.command)
                showToast({
                  type: 'success',
                  message: `已复制命令: ${contextMenu.tab!.api.command}`,
                })
                setContextMenu((prev) => ({ ...prev, visible: false }))
              }}
              className="flex w-full items-center gap-2 px-2.5 py-1.5 rounded-lg text-slate-700 dark:text-slate-300 hover:bg-slate-100 dark:hover:bg-slate-800 hover:text-slate-900 dark:hover:text-white cursor-pointer"
            >
              <Copy className="h-3.5 w-3.5 text-slate-400 dark:text-slate-500" />
              <span>复制 Command</span>
            </button>

            {contextMenu.tab.api.doc_url && (
              <button
                type="button"
                onClick={() => {
                  window.open(contextMenu.tab!.api.doc_url, '_blank')
                  setContextMenu((prev) => ({ ...prev, visible: false }))
                }}
                className="flex w-full items-center gap-2 px-2.5 py-1.5 rounded-lg text-slate-700 dark:text-slate-300 hover:bg-slate-100 dark:hover:bg-slate-800 hover:text-slate-900 dark:hover:text-white cursor-pointer"
              >
                <ExternalLink className="h-3.5 w-3.5 text-slate-400 dark:text-slate-500" />
                <span>查看官方文档</span>
              </button>
            )}
          </motion.div>
        )}
      </AnimatePresence>

      {/* 接口调试面板 - 沉浸式文档放大阅读弹窗 */}
      {isDocsModalOpen && activeApi && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/60 backdrop-blur-xs p-2 sm:p-6 animate-in fade-in duration-150">
          <div className="w-full max-w-6xl h-[92vh] bg-white dark:bg-slate-900 rounded-2xl shadow-2xl border border-slate-200 dark:border-slate-800 flex flex-col overflow-hidden animate-in zoom-in-95 duration-150">
            <div className="p-4 border-b border-slate-200 dark:border-slate-800 bg-slate-50/90 dark:bg-slate-950/80 flex items-center justify-between">
              <div className="flex items-center gap-2.5 min-w-0">
                <BookOpen className="h-4 w-4 text-orange-500 shrink-0" />
                <h3 className="font-bold text-slate-900 dark:text-slate-100 text-sm truncate">
                  {activeApi.name} - 沉浸式接口文档
                </h3>
                <span className="font-mono text-xs text-slate-500 dark:text-slate-400 bg-slate-100 dark:bg-slate-800 px-2 py-0.5 rounded border border-slate-200 dark:border-slate-700 shrink-0 hidden sm:inline">
                  {activeApi.command}
                </span>
              </div>
              <div className="flex items-center gap-2">
                <Button
                  size="sm"
                  onClick={() => setIsDocsModalOpen(false)}
                  variant="ghost"
                  className="h-7 px-2.5 text-xs text-slate-500 dark:text-slate-400 hover:text-slate-800 dark:hover:text-slate-200 gap-1 cursor-pointer"
                >
                  <Minimize2 className="h-3.5 w-3.5" />
                  <span>退出放大 (ESC)</span>
                </Button>
                <button
                  onClick={() => setIsDocsModalOpen(false)}
                  className="p-1 rounded-lg text-slate-400 hover:text-slate-600 dark:hover:text-slate-200 hover:bg-slate-200/60 dark:hover:bg-slate-800 cursor-pointer"
                >
                  <X className="h-4 w-4" />
                </button>
              </div>
            </div>
            <div className="flex-1 overflow-y-auto p-6 bg-slate-50/40 dark:bg-slate-950/60">
              <ApiDocsViewer
                api={activeApi}
                onApplyExample={(ex) => {
                  handleApplyDocExample(ex)
                  setIsDocsModalOpen(false)
                }}
                onToggleExpand={() => setIsDocsModalOpen(false)}
                isExpanded={true}
              />
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

export default ApiWorkbenchPanel
