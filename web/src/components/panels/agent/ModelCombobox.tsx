import { useEffect, useRef, useState } from 'react'
import { Check, ChevronDown, Loader2 } from 'lucide-react'
import { Button } from '@/components/ui/button'

type Props = {
  value: string
  models: string[]
  onChange: (value: string) => void
  disabled: boolean
  invalid: boolean
  inputClassName: string
  fetching: boolean
  onFetch: () => void
}

// Unlike datalist, opening this control shows every fetched model, even when
// the configured model is absent from the provider's current list.
export function ModelCombobox({ value, models, onChange, disabled, invalid, inputClassName, fetching, onFetch }: Props) {
  const rootRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const [active, setActive] = useState(-1)
  const options = query ? models.filter((model) => model.toLowerCase().includes(query.toLowerCase())) : models
  const expanded = open && !disabled && models.length > 0

  useEffect(() => {
    const show = models.length > 0 && !!rootRef.current?.contains(document.activeElement)
    setOpen(show)
    setQuery('')
    setActive(-1)
    if (show) inputRef.current?.focus({ preventScroll: true })
  }, [models])

  useEffect(() => {
    if (!expanded) return
    const closeOutside = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false)
    }
    document.addEventListener('pointerdown', closeOutside)
    return () => document.removeEventListener('pointerdown', closeOutside)
  }, [expanded])

  useEffect(() => {
    if (expanded && active >= 0) document.getElementById(`model-option-${active}`)?.scrollIntoView({ block: 'nearest' })
  }, [expanded, active, query])

  function showAll() {
    setQuery('')
    setActive(-1)
    setOpen(true)
  }

  function choose(name: string) {
    onChange(name)
    setOpen(false)
    setQuery('')
    inputRef.current?.focus({ preventScroll: true })
  }

  return <div ref={rootRef} className="flex min-w-0 items-start gap-2"
    onBlur={(event) => { if (!event.currentTarget.contains(event.relatedTarget)) setOpen(false) }}
    onKeyDown={(event) => {
      if (event.nativeEvent.isComposing) return
      if (event.key === 'Escape' && expanded) {
        event.preventDefault()
        event.stopPropagation()
        setOpen(false)
      }
    }}>
    <div className="relative min-w-0 flex-1"><input ref={inputRef} id="model-name" role="combobox" aria-autocomplete="list"
      aria-expanded={expanded} aria-controls="settings-models"
      aria-activedescendant={expanded && active >= 0 && options[active] ? `model-option-${active}` : undefined}
      aria-describedby="model-choice-hint" aria-invalid={invalid} disabled={disabled}
      value={value} maxLength={256} autoComplete="off" spellCheck={false}
      placeholder="输入或选择模型标识" className={`${inputClassName} pr-10`}
      onClick={showAll}
      onChange={(event) => { onChange(event.target.value); setQuery(event.target.value); setActive(-1); setOpen(true) }}
      onKeyDown={(event) => {
        if (event.nativeEvent.isComposing) return
        if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
          if (!models.length) return
          event.preventDefault()
          const nextOptions = expanded ? options : models
          if (!expanded) showAll()
          setActive((current) => nextOptions.length ?
            (event.key === 'ArrowDown' ? (expanded ? current + 1 : 0) : (expanded && current >= 0 ? current - 1 + nextOptions.length : nextOptions.length - 1)) % nextOptions.length : -1)
        } else if (event.key === 'Enter' && expanded) {
          event.preventDefault()
          if (active >= 0 && options[active]) choose(options[active])
          else setOpen(false)
        } else if (event.key === 'Tab') setOpen(false)
      }} />
    <button type="button" aria-label="展开模型列表" aria-controls="settings-models" aria-expanded={expanded}
      disabled={disabled || models.length === 0} onMouseDown={(event) => event.preventDefault()}
      onClick={() => { if (expanded) setOpen(false); else showAll(); inputRef.current?.focus({ preventScroll: true }) }}
      className="absolute inset-y-0 right-0 flex w-10 items-center justify-center rounded-r-md text-slate-600 hover:bg-slate-100 focus-visible:outline-2 focus-visible:outline-blue-500 disabled:opacity-40 dark:text-slate-300 dark:hover:bg-slate-800">
      <ChevronDown className="h-4 w-4" />
    </button>
    {expanded && <div className="absolute inset-x-0 top-full z-20 mt-1 overflow-hidden rounded-md border border-slate-300 bg-white shadow-sm dark:border-slate-700 dark:bg-slate-950">
      <div className="border-b border-slate-200 px-3 py-2 text-xs text-slate-600 dark:border-slate-800 dark:text-slate-300">{query ? `${options.length} / ${models.length}` : models.length} 个模型</div>
      <ul id="settings-models" role="listbox" aria-label="可用模型" className="max-h-60 overflow-y-auto py-1">
        {options.map((name, index) => <li key={name} id={`model-option-${index}`} role="option" aria-selected={name === value}
          onMouseDown={(event) => event.preventDefault()} onClick={() => choose(name)}
          className={`flex cursor-pointer items-start gap-2 px-3 py-2 text-sm break-all hover:bg-slate-100 dark:hover:bg-slate-800 ${active === index ? 'bg-slate-100 dark:bg-slate-800' : ''}`}>
          <span className="min-w-0 flex-1">{name}</span>{name === value && <Check aria-hidden="true" className="mt-0.5 h-4 w-4 shrink-0" />}
        </li>)}
      </ul>
      {options.length === 0 && <p role="status" className="px-3 py-2 text-sm text-slate-600 dark:text-slate-300">没有匹配模型，可继续使用手动输入的标识。</p>}
    </div>}</div>
    <Button type="button" variant="outline" className="h-[38px] shrink-0 rounded-md px-3" disabled={disabled || fetching}
      onClick={() => { setOpen(false); inputRef.current?.focus({ preventScroll: true }); onFetch() }}>
      {fetching && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}{fetching ? '获取中……' : '获取模型列表'}
    </Button>
  </div>
}
