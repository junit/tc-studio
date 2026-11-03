import React, { useEffect, useMemo, useState, useRef } from 'react'
import { marked } from 'marked'
import hljs from 'highlight.js'
import 'highlight.js/styles/atom-one-dark.css'
import { Copy, Check } from 'lucide-react'
import { EChartsViewer } from '@/components/agent/EChartsViewer'
import { fetchAuthenticatedBlob } from '@/lib/auth'

interface MarkdownViewProps {
  content: string
  className?: string
  copyable?: boolean
  copyMode?: 'header' | 'floating' | 'none'
  title?: string
  containerClassName?: string
}

const escapeHtml = (value: unknown): string =>
  String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')

const sanitizeHref = (value: unknown): string => {
  const href = String(value ?? '').trim()
  if (!href || href.startsWith('//')) return '#'
  if (/^(https?:|mailto:)/i.test(href)) return href
  if (/^[a-z][a-z0-9+.-]*:/i.test(href)) return '#'
  return href.startsWith('/') || href.startsWith('./') || href.startsWith('../') || href.startsWith('#')
    ? href
    : '#'
}
const sanitizeImageSrc = (value: unknown): string => {
  const src = String(value ?? '').trim()
  if (/^data:/i.test(src)) {
    const isSafeBase64Image = /^data:image\/(?:png|jpe?g|gif|webp);base64,[a-z0-9+/]+={0,2}$/i.test(src)
    return isSafeBase64Image && src.length <= 10 * 1024 * 1024 ? src : '#'
  }
  return sanitizeHref(src)
}


const markdownImageCache = new Map<string, { url?: string; promise?: Promise<string>; lastUsed: number; consumers: number }>()
const MAX_MARKDOWN_IMAGE_CACHE = 32

// 引用计数：被挂载中的 <img> 消费中的 URL 不参与驱逐回收，
// 避免 LRU 驱逐时 revoke 掉仍在页面上显示的图片
function acquireMarkdownImage(src: string): void {
  const entry = markdownImageCache.get(src)
  if (entry) {
    entry.consumers += 1
  } else {
    markdownImageCache.set(src, { lastUsed: Date.now(), consumers: 1 })
  }
}

function releaseMarkdownImage(src: string): void {
  const entry = markdownImageCache.get(src)
  if (entry && entry.consumers > 0) entry.consumers -= 1
}

function loadCachedMarkdownImage(src: string): Promise<string> {
  const now = Date.now()
  const cached = markdownImageCache.get(src)
  if (cached?.url) {
    cached.lastUsed = now
    return Promise.resolve(cached.url)
  }
  if (cached?.promise) return cached.promise

  const entry = cached ?? ({ lastUsed: now, consumers: 0 } as { url?: string; promise?: Promise<string>; lastUsed: number; consumers: number })
  entry.lastUsed = now
  entry.promise = fetchAuthenticatedBlob(src).then((blob) => {
    const objectUrl = URL.createObjectURL(blob)
    entry.url = objectUrl
    entry.promise = undefined
    entry.lastUsed = Date.now()
    return objectUrl
  }).catch((error) => {
    markdownImageCache.delete(src)
    throw error
  })
  if (!cached) markdownImageCache.set(src, entry)

  while (markdownImageCache.size > MAX_MARKDOWN_IMAGE_CACHE) {
    const oldest = [...markdownImageCache.entries()]
      .filter(([, value]) => !value.promise && value.consumers <= 0)
      .sort(([, a], [, b]) => a.lastUsed - b.lastUsed)[0]
    if (!oldest) break
    URL.revokeObjectURL(oldest[1].url || '')
    markdownImageCache.delete(oldest[0])
  }
  return entry.promise
}


// 配置 marked 解析选项：支持换行、GFM 表格与 highlight.js 代码语法高亮与代码块独立复制
marked.setOptions({
  breaks: true,
  gfm: true,
})

marked.use({
  renderer: {
    html({ text }: { text: string }) {
      return escapeHtml(text)
    },
    code({ text, lang }: { text: string; lang?: string }) {
      const cleanLang = (lang || '').trim().toLowerCase()
      const validLang = Boolean(/^[a-z0-9_+-]{1,32}$/.test(cleanLang) && hljs.getLanguage(cleanLang))
      let highlighted = escapeHtml(text)
      try {
        highlighted = validLang
          ? hljs.highlight(text, { language: cleanLang }).value
          : hljs.highlightAuto(text).value
      } catch {
        highlighted = escapeHtml(text)
      }
      const encoded = encodeURIComponent(text)
      return `
        <div class="code-block-wrapper relative group/code my-2.5 rounded-xl border border-slate-200 dark:border-slate-800 bg-slate-50/90 dark:bg-[#0b0f19] overflow-hidden shadow-2xs">
          <div class="flex items-center justify-between px-3 py-1.5 bg-slate-100/90 dark:bg-slate-900/90 text-[11px] text-slate-500 dark:text-slate-400 font-mono border-b border-slate-200 dark:border-slate-800/80 select-none">
            <span class="font-semibold text-slate-700 dark:text-slate-300 uppercase tracking-wider">${escapeHtml(cleanLang || 'code')}</span>
            <button type="button" class="markdown-code-copy-btn flex items-center gap-1 px-2 py-0.5 rounded-md hover:bg-slate-200/80 dark:hover:bg-slate-800 text-slate-500 dark:text-slate-400 hover:text-slate-800 dark:hover:text-slate-100 transition-colors cursor-pointer text-[10px]" data-code="${encoded}">
              <svg class="h-3 w-3 copy-icon" xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect width="14" height="14" x="8" y="8" rx="2" ry="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/></svg>
              <span class="copy-label">复制代码</span>
            </button>
          </div>
          <pre class="hljs-pre !my-0 !rounded-none !border-0 bg-slate-50 text-slate-800 dark:bg-[#0b0f19] dark:text-[#e2e8f0]"><code class="hljs ${validLang ? `language-${escapeHtml(cleanLang)}` : ''}">${highlighted}</code></pre>
        </div>
      `
    },
    image({ href, title, text }: { href: string; title?: string | null; text: string }) {
      const safeSrc = sanitizeImageSrc(href)
      const placeholder = 'data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs='
      const titleAttr = title ? ` title="${escapeHtml(title)}"` : ''
      const isAuthenticatedApiImage = safeSrc.startsWith('/api/')
      const imageSrc = isAuthenticatedApiImage ? placeholder : safeSrc
      const dataSrcAttr = isAuthenticatedApiImage ? ` data-src="${escapeHtml(safeSrc)}"` : ''
      return `<img src="${imageSrc}"${dataSrcAttr} alt="${escapeHtml(text)}"${titleAttr} class="max-w-full rounded-lg" />`
    },
    link({ href, title, text }: { href: string; title?: string | null; text: string }) {
      const safeHref = sanitizeHref(href)
      const isDangerousProtocol = safeHref === '#'

      const isFileDownload = Boolean(safeHref && !isDangerousProtocol && (
          (safeHref.includes('/api/agent/bots/') && safeHref.includes('/files/')) ||
          /\.(xlsx|xls|csv|zip|pdf|docx|parquet)(\?.*)?$/i.test(safeHref) ||
          safeHref.includes('download=true')
        ))
      const titleAttr = title ? ` title="${escapeHtml(title)}"` : ''
      const safeText = escapeHtml(text)
      if (isFileDownload) {
        return `<a href="${escapeHtml(safeHref)}" download target="_blank" rel="noopener noreferrer"${titleAttr} class="inline-flex items-center gap-1.5 px-2.5 py-1 my-1 rounded-lg font-medium text-xs text-blue-600 dark:text-blue-400 bg-blue-50/90 dark:bg-blue-950/60 hover:bg-blue-100 dark:hover:bg-blue-900/60 border border-blue-200 dark:border-blue-800 transition-colors shadow-2xs no-underline cursor-pointer"><svg class="w-3.5 h-3.5 shrink-0 inline-block text-blue-500" xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" x2="12" y1="15" y2="3"/></svg><span>${safeText}</span></a>`
      }
      return `<a href="${escapeHtml(safeHref)}" target="_blank" rel="noopener noreferrer"${titleAttr} class="text-blue-600 dark:text-blue-400 hover:underline font-medium">${safeText}</a>`
    },
  },
})

const SafeMarkdownHtml: React.FC<{ html: string; className?: string }> = ({ html, className = '' }) => {
  const containerRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const container = containerRef.current
    if (!container) return
    const parsed = new DOMParser().parseFromString(`<div>${html}</div>`, 'text/html')
    const root = parsed.body.firstElementChild
    if (!root) return

    const blockedTags = new Set(['base', 'embed', 'form', 'iframe', 'link', 'meta', 'object', 'script', 'style'])
    for (const element of Array.from(root.querySelectorAll('*'))) {
      if (blockedTags.has(element.tagName.toLowerCase())) {
        element.remove()
        continue
      }
      for (const attribute of Array.from(element.attributes)) {
        const name = attribute.name.toLowerCase()
        if (name.startsWith('on') || name === 'srcdoc' || name === 'style') {
          element.removeAttribute(attribute.name)
          continue
        }
        if (['action', 'formaction', 'href', 'src', 'xlink:href'].includes(name)) {
          const safeValue = name === 'src' ? sanitizeImageSrc(attribute.value) : sanitizeHref(attribute.value)
          element.setAttribute(attribute.name, safeValue)
        }
      }
    }

    const fragment = document.createDocumentFragment()
    while (root.firstChild) {
      fragment.appendChild(document.importNode(root.firstChild, true))
      root.removeChild(root.firstChild)
    }
    container.replaceChildren(fragment)
  }, [html])

  useEffect(() => {
    const container = containerRef.current
    if (!container) return
    let cancelled = false
    const acquired: string[] = []
    const imageElements = Array.from(container.querySelectorAll('img[data-src]'))
      imageElements.forEach((image) => {
        const src = image.getAttribute('data-src') || ''
        if (!src.startsWith('/api/')) return
        acquireMarkdownImage(src)
        acquired.push(src)
        loadCachedMarkdownImage(src)
          .then((objectUrl) => {
            if (cancelled) return
            image.removeAttribute('data-src')
            image.setAttribute('src', objectUrl)
          })
          .catch(() => {
            if (cancelled) return
            const fallback = document.createElement('a')
            fallback.href = src
            fallback.download = ''
            fallback.textContent = `图片未内联预览：${image.getAttribute('alt') || '点击下载查看'}`
            fallback.className = 'inline-flex items-center rounded-md border border-amber-200 bg-amber-50 px-2 py-1 text-xs text-amber-700 no-underline'
            image.replaceWith(fallback)
          })
      })
    return () => {
      cancelled = true
      acquired.forEach(releaseMarkdownImage)
    }
  }, [html])

  return <div ref={containerRef} className={className} />
}
export const MarkdownView: React.FC<MarkdownViewProps> = ({
  content,
  className = '',
  copyable = false,
  copyMode = 'header',
  title = '',
  containerClassName = '',
}) => {
  const [isCopied, setIsCopied] = useState<boolean>(false)
  const containerRef = useRef<HTMLDivElement>(null)

  // 预处理内容：清洗模型内部协议残留，并将多行 <code>...</code> 规范化为 Markdown 代码块
  const preprocessedContent = useMemo(() => {
    if (!content) return ''
    let cleaned = content

    // 1. 过滤任何泄漏到正文中的 Calling tools: [...] 内部调试调用
    cleaned = cleaned.replace(/Calling tools:\s*\[[\s\S]*?\]/g, '').trim()

    // 2. 过滤任何残留的 DeepSeek 原生 DSML 工具标记
    cleaned = cleaned.replace(/<｜｜DSML｜｜\s*calls>[\s\S]*?(?:<\/invoke>|$)/g, '').trim()
    cleaned = cleaned.replace(/<parameter name=["']code["']>([\s\S]*?)<\/parameter>/gi, '\n```python\n$1\n```\n')

    // 3. 将大模型可能输出的 <code>...</code> 标签规范化为 Markdown 代码块 ```python
    cleaned = cleaned.replace(/<code>\s*([\s\S]*?)\s*<\/code>/gi, (_match, p1) => {
      const code = p1.trim()
      if (code.includes('\n') || code.includes('import ') || code.includes('def ') || code.includes(' = ')) {
        return `\n\`\`\`python\n${code}\n\`\`\`\n`
      }
      return `\`${code}\``
    })

    return cleaned
  }, [content])

  // 检查是否包含 ```echarts 或 ```chart 语法块，支持代码块就地交互式图表渲染
  const segments = useMemo(() => {
    if (!preprocessedContent) return []
    const regex = /```(?:echarts|chart)\s*\n([\s\S]*?)\n```/gi
    let match
    let lastIndex = 0
    const list: Array<{ type: 'markdown' | 'echarts'; text: string }> = []

    while ((match = regex.exec(preprocessedContent)) !== null) {
      const matchStart = match.index
      const matchEnd = regex.lastIndex
      if (matchStart > lastIndex) {
        list.push({
          type: 'markdown',
          text: preprocessedContent.slice(lastIndex, matchStart),
        })
      }
      list.push({
        type: 'echarts',
        text: match[1],
      })
      lastIndex = matchEnd
    }

    if (lastIndex < preprocessedContent.length) {
      list.push({
        type: 'markdown',
        text: preprocessedContent.slice(lastIndex),
      })
    }

    return list
  }, [preprocessedContent])

  const htmlContent = useMemo(() => {
    if (!preprocessedContent || segments.some((s) => s.type === 'echarts')) return ''
    try {
      return marked.parse(preprocessedContent) as string
    } catch (e) {
      console.error('Failed to parse markdown:', e)
      return preprocessedContent
    }
  }, [preprocessedContent, segments])

  const handleCopy = (e: React.MouseEvent) => {
    e.stopPropagation()
    if (!preprocessedContent) return
    navigator.clipboard.writeText(preprocessedContent)
    setIsCopied(true)
    setTimeout(() => setIsCopied(false), 2000)
  }

  // 捕获内部各个代码块自带的复制按钮点击事件
  const handleContainerClick = (e: React.MouseEvent<HTMLDivElement>) => {
    const target = (e.target as HTMLElement).closest('.markdown-code-copy-btn') as HTMLElement | null
    if (!target) return
    e.stopPropagation()
    const rawCode = target.getAttribute('data-code')
    if (rawCode) {
      try {
        const code = decodeURIComponent(rawCode)
        navigator.clipboard.writeText(code)
        const label = target.querySelector('.copy-label')
        if (label) {
          label.textContent = '已复制'
          target.classList.add('text-emerald-400')
          setTimeout(() => {
            label.textContent = '复制代码'
            target.classList.remove('text-emerald-400')
          }, 2000)
        }
      } catch (err) {
        console.error('Failed to copy code snippet:', err)
      }
    }
  }

  return (
    <div
      ref={containerRef}
      onClick={handleContainerClick}
      className={`relative group/markdown ${containerClassName}`}
    >
      {/* 模式 1: 独立 Header 工具条（位于正文上方，彻底避免遮挡正文） */}
      {copyable && content && copyMode === 'header' && (
        <div className="flex items-center justify-between px-3 py-1.5 bg-slate-100/90 dark:bg-slate-800/90 border-b border-slate-200/90 dark:border-slate-800 text-[11px] font-medium text-slate-500 dark:text-slate-400 select-none rounded-t-xl">
          <span>{title || '正文内容'}</span>
          <button
            type="button"
            onClick={handleCopy}
            className="flex items-center gap-1 px-2 py-0.5 rounded-md hover:bg-slate-200 dark:hover:bg-slate-700 text-slate-600 dark:text-slate-300 transition-colors cursor-pointer"
          >
            {isCopied ? (
              <>
                <Check className="h-3 w-3 text-emerald-500" />
                <span className="text-emerald-600 dark:text-emerald-400 text-[11px] font-semibold">已复制</span>
              </>
            ) : (
              <>
                <Copy className="h-3 w-3 text-slate-400" />
                <span className="text-[11px]">复制</span>
              </>
            )}
          </button>
        </div>
      )}

      {/* 模式 2: 浮动复制按钮（预留右侧 pr-14 安全边距，杜绝文字重叠） */}
      {copyable && content && copyMode === 'floating' && (
        <button
          type="button"
          onClick={handleCopy}
          className="absolute top-2 right-2 z-10 px-2 py-1 rounded-lg bg-white/95 dark:bg-slate-900/95 hover:bg-white dark:hover:bg-slate-800 text-slate-500 hover:text-slate-800 dark:text-slate-400 dark:hover:text-slate-100 border border-slate-200/90 dark:border-slate-800 shadow-xs backdrop-blur-xs flex items-center gap-1.5 text-xs font-medium cursor-pointer transition-all opacity-80 hover:opacity-100"
          title="复制全部正文"
        >
          {isCopied ? (
            <>
              <Check className="h-3 w-3 text-emerald-500" />
              <span className="text-emerald-600 dark:text-emerald-400 font-semibold text-[11px]">已复制</span>
            </>
          ) : (
            <>
              <Copy className="h-3 w-3 text-slate-400" />
              <span className="text-[11px]">复制</span>
            </>
          )}
        </button>
      )}

      {segments.some((s) => s.type === 'echarts') ? (
        <div className={`space-y-3 ${copyable && copyMode === 'floating' ? 'pr-16' : ''} ${className}`}>
          {segments.map((seg, idx) => {
            if (seg.type === 'echarts') {
              return <EChartsViewer key={idx} code={seg.text} />
            }
            let html = ''
            try {
              html = marked.parse(seg.text) as string
            } catch {
              html = seg.text
            }
            if (!html || !html.trim()) return null
            return (
              <SafeMarkdownHtml
                key={idx}
                html={html}
                className={`markdown-content ${className}`}
              />
            )
          })}
        </div>
      ) : (
        <SafeMarkdownHtml html={htmlContent} className={`markdown-content ${copyable && copyMode === 'floating' ? 'pr-16' : ''} ${className}`} />
      )}
    </div>
  )
}
