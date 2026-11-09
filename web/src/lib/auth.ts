import { readTcStorage } from './storage'

export function getAccessToken(): string {
  const sessionToken = readTcStorage(sessionStorage, 'access_token') || ''
  if (sessionToken) return sessionToken
  if (import.meta.env.DEV) return import.meta.env.VITE_OIDC_DEV_ACCESS_TOKEN || ''
  return ''
}

export function authHeaders(init?: HeadersInit): Headers {
  const headers = new Headers(init)
  const token = getAccessToken()
  if (token && !headers.has('Authorization')) {
    headers.set('Authorization', `Bearer ${token}`)
  }
  return headers
}

export async function fetchAuthenticatedBlob(url: string, maxBytes = 8 * 1024 * 1024): Promise<Blob> {
  // 与 downloadWithAuth/openWithAuth 同源同源限制一致，避免 Bearer Token 意外跨源外带
  const safeUrl = requireSameOriginApiUrl(url)
  const response = await fetch(safeUrl, { headers: authHeaders() })
  if (!response.ok) throw new Error(`资源加载失败 (${response.status})`)
  const declaredLength = Number(response.headers.get('content-length') || 0)
  if (declaredLength > maxBytes) throw new Error('资源过大，已跳过内联预览')
  if (!response.body) return response.blob()
  const reader = response.body.getReader()
  const chunks: BlobPart[] = []
  let total = 0
  while (true) {
    const { value, done } = await reader.read()
    if (done) break
    if (!value) continue
    total += value.byteLength
    if (total > maxBytes) {
      await reader.cancel()
      throw new Error('资源过大，已跳过内联预览')
    }
    chunks.push(value)
  }
  return new Blob(chunks, { type: response.headers.get('content-type') || 'application/octet-stream' })
}


function requireSameOriginApiUrl(url: string): string {
  const parsed = new URL(url, window.location.origin)
  if (parsed.origin !== window.location.origin || !parsed.pathname.startsWith('/api/')) {
    throw new Error('仅允许访问本站 API 地址')
  }
  return parsed.href
}

export async function downloadWithAuth(url: string, filename?: string): Promise<void> {
  const safeUrl = requireSameOriginApiUrl(url)
  const response = await fetch(safeUrl, { headers: authHeaders() })
  if (!response.ok) throw new Error(`下载失败 (${response.status})`)
  const blob = await response.blob()
  const objectUrl = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = objectUrl
  anchor.download = filename || ''
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  window.setTimeout(() => URL.revokeObjectURL(objectUrl), 30_000)
}

export async function openWithAuth(url: string): Promise<void> {
  const safeUrl = requireSameOriginApiUrl(url)
  const response = await fetch(safeUrl, { headers: authHeaders() })
  if (!response.ok) throw new Error(`打开失败 (${response.status})`)
  let blob = await response.blob()
  if (blob.type.includes('text/html')) {
    const markdownUrl = new URL(safeUrl, window.location.origin)
    markdownUrl.searchParams.set('format', 'markdown')
    markdownUrl.searchParams.delete('preview')
    const markdownBlob = await fetchAuthenticatedBlob(markdownUrl.href)
    const markdownBlobUrl = URL.createObjectURL(markdownBlob)
    const html = await blob.text()
    blob = new Blob([html.replace(/href=["']\?format=markdown["']/g, `href="${markdownBlobUrl}"`)], { type: 'text/html' })
    // 与主 blob 同周期回收，避免 Markdown 预览的二级 Blob URL 永久滞留
    window.setTimeout(() => URL.revokeObjectURL(markdownBlobUrl), 60_000)
  }
  const blobUrl = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = blobUrl
  anchor.target = '_blank'
  anchor.rel = 'noopener noreferrer'
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  window.setTimeout(() => URL.revokeObjectURL(blobUrl), 60_000)
}

export function installApiAuthInterceptor(): void {
  const nativeFetch = window.fetch.bind(window)
  window.fetch = (input: RequestInfo | URL, init?: RequestInit) => {
    const requestUrl = new URL(input instanceof Request ? input.url : input.toString(), window.location.origin)
    const isSameOriginApi = requestUrl.origin === window.location.origin && requestUrl.pathname.startsWith('/api/')
    const existingHeaders = init?.headers || (input instanceof Request ? input.headers : undefined)
    const requestInit: RequestInit = isSameOriginApi
      ? { ...init, headers: authHeaders(existingHeaders) }
      : init
    return nativeFetch(input, requestInit)
  }

  document.addEventListener('click', (event) => {
    const target = event.target as HTMLElement | null
    const anchor = target?.closest('a')
    if (!anchor || event.defaultPrevented || event.button !== 0) return
    if (!anchor.hasAttribute('download')) return
    const href = anchor.getAttribute('href')
    if (!href || !href.startsWith('/api/')) return
    event.preventDefault()
    downloadWithAuth(href, anchor.getAttribute('download') || undefined).catch((error) => {
      console.error('Authenticated download failed:', error)
    })
  })
}
