(() => {
  sessionStorage.setItem('__auditBootCount', String(Number(sessionStorage.getItem('__auditBootCount') || 0) + 1))
  const originalFetch = window.fetch.bind(window)
  const makeBot = (id, name, visibility = 'public') => ({
    id, name, visibility, avatar: '🤖', owner_id: visibility === 'private' ? 'audit-user' : null,
    description: 'Synthetic isolation audit fixture', base_prompt: 'synthetic test prompt',
    learned_guidelines: [], skills: [], memories: [], suggested_prompts: ['synthetic suggestion'],
    skills_count: 0, memories_count: 0, guidelines_count: 0, files_count: 0,
    permissions: { can_edit_definition: false },
  })
  const publicBot = makeBot('default_assistant', 'AUDIT_PUBLIC_A')
  const privateBot = makeBot('audit_private_b', 'AUDIT_PRIVATE_B', 'private')
  const sidebarMode = new URLSearchParams(location.search).has('audit-sidebar')
  const streamCase = new URLSearchParams(location.search).get('audit-stream')
  const capabilityMode = new URLSearchParams(location.search).has('audit-capabilities')
  let modelConfig = { provider: 'ollama', model_name: 'synthetic', base_url: 'http://127.0.0.1:11434', temperature: 0.2,
    max_steps: 30, context_budget: 32000, max_context_turns: 25, has_api_key: true, api_key_masked: '***' }
  window.__auditSettingsWrites = []
  const capabilityCatalog = {
    version: 0, catalog_version: 'synthetic-catalog', disabled_ids: [], enabled_ids: [],
    web_read: { allowed_domains: [], allowed_ports: [443], connect_timeout_seconds: 3, total_timeout_seconds: 10,
      max_response_bytes: 524288, max_text_chars: 40000, max_concurrent_requests: 2, requests_per_minute: 20 },
    capabilities: [
      { id: 'files', title: '文件读写与发布', description: '读取当前用户文件，保存并发布任务产出物。', contexts: ['agent', 'delegate', 'skill'], requires: [], enabled: {} },
      { id: 'pdf', title: 'PDF 报告', description: '生成支持中文排版的 PDF 报告，依赖文件读写能力。', contexts: ['agent', 'delegate', 'skill'], requires: ['files'], enabled: {} },
      { id: 'cloud', title: '云 API', description: '检索接口文档并调用已注册的云服务；仍受权限与审批控制。', contexts: ['agent', 'delegate', 'skill'], requires: [], enabled: {} },
      { id: 'web_read', title: '网页读取', description: '读取批准域名的网页文本，默认关闭。', contexts: ['agent', 'delegate', 'skill'], requires: [], enabled: {}, default_enabled: false },
    ],
  }
  window.__auditStreamFinished = false
  const streamResponse = () => {
    const error = 'SYNTHETIC_MODEL_DEPENDENCY_ERROR'
    const snapshot = { run: { status: 'error', stream_status: 'error', error } }
    const cases = {
      error: [`event: sync\ndata: {"run":{"status":"running","stream_status":"thinking"}}\n\nevent: error\ndata: ${JSON.stringify({ error })}\n\n`],
      'split-error': ['event: error\n', `data: ${JSON.stringify({ error })}\n\n`],
      'sync-error': [`event: sync\ndata: ${JSON.stringify(snapshot)}\n\nevent: error\ndata: ${JSON.stringify(snapshot)}\n\n`],
      truncated: ['event: chunk\ndata: {"delta":"SYNTHETIC_PARTIAL_ANSWER"}\n\n'],
      done: ['event: chunk\ndata: {"delta":"SYNTHETIC_COMPLETE_ANSWER"}\n\nevent: done\ndata: {}\n\n'],
    }
    const chunks = cases[streamCase]
    if (!chunks) throw new Error(`Unknown synthetic stream case: ${streamCase}`)
    const encoder = new TextEncoder()
    let index = 0
    return new Response(new ReadableStream({
      async pull(controller) {
        if (index < chunks.length) {
          // A separate task forces event/data to arrive in different reads.
          if (index > 0) await new Promise(resolve => setTimeout(resolve, 20))
          controller.enqueue(encoder.encode(chunks[index++]))
        } else {
          controller.close()
          window.__auditStreamFinished = true
        }
      },
    }), { status: 200, headers: { 'Content-Type': 'text/event-stream' } })
  }
  if (sidebarMode) {
    privateBot.last_message = { role: 'user', content: 'SIDEBAR_LAST_MESSAGE', timestamp: '2026-11-12T05:00:00Z' }
    window.__auditRunStatus = 'thinking'
  }
  window.__auditPendingSearch = null
  window.__auditPendingHistory = null
  window.__auditRequests = []
  const response = value => new Response(JSON.stringify(value), { status: 200, headers: { 'Content-Type': 'application/json' } })
  window.fetch = async (input, init = {}) => {
    const url = new URL(input instanceof Request ? input.url : String(input), location.origin)
    if (!url.pathname.startsWith('/api/')) return originalFetch(input, init)
    window.__auditRequests.push(url.pathname + url.search)
    const path = url.pathname
    if (streamCase && path.endsWith('/chat') && init.method === 'POST') return streamResponse()
    if (path === '/api/auth/me') return response({ subject: 'audit-user', is_admin: capabilityMode, is_operator: true, auth_mode: 'oidc' })
    if (path === '/api/agent/capabilities' && capabilityMode) {
      if (init.method === 'PUT') {
        window.__auditSettingsWrites.push('policy')
        if (window.__auditCapabilitySaveError) throw new Error('SYNTHETIC_SAVE_NETWORK_ERROR')
        window.__auditCapabilityPayload = JSON.parse(init.body)
        if (window.__auditCapabilityConflict) return new Response(JSON.stringify({ detail: 'SYNTHETIC_VERSION_CONFLICT' }), { status: 409, headers: { 'Content-Type': 'application/json' } })
        capabilityCatalog.disabled_ids = window.__auditCapabilityPayload.disabled_ids
        capabilityCatalog.enabled_ids = window.__auditCapabilityPayload.enabled_ids
        capabilityCatalog.web_read = window.__auditCapabilityPayload.web_read
        capabilityCatalog.version += 1
      } else if (window.__auditCapabilityLoadError) throw new Error('SYNTHETIC_LOAD_NETWORK_ERROR')
      return response(capabilityCatalog)
    }
    if (path === '/api/services') return response({ services: [{ id: 'im', name: 'IM', api_count: 1 }] })
    if (path === '/api/apis') return response({ apis: [] })
    if (path === '/api/agent/bots') return response({ bots: [publicBot, privateBot] })
    if (path === '/api/agent/active-runs') return response({ active_runs: sidebarMode && window.__auditRunStatus ? {
      audit_private_b: { status: 'running', stream_status: window.__auditRunStatus, current_step: 3 },
    } : {} })
    if (path.endsWith('/active-run')) return response({ has_active_run: false, run: null })
    if (path.endsWith('/knowledge/docs')) return response({ docs: [] })
    if (path.endsWith('/files')) return response({ files: [] })
    if (path.endsWith('/history')) {
      if (url.searchParams.has('before_id')) return new Promise(resolve => {
        window.__auditPendingHistory = () => resolve(response({
          history: [{ id: 'old-a', role: 'user', content: 'A_DELAYED_HISTORY_SECRET', metadata: {} }], total: 2, has_more: false,
        }))
      })
      const isPublic = path.includes('default_assistant')
      return response({ history: [{ id: isPublic ? 'a' : 'b', role: 'user', content: isPublic ? 'A_CURRENT_HISTORY' : 'B_CURRENT_HISTORY', metadata: {} }], has_more: isPublic, total: isPublic ? 2 : 1 })
    }
    if (path.endsWith('/knowledge/search')) return new Promise(resolve => {
      window.__auditPendingSearch = () => resolve(response({ results: [{
        doc_id: 'a-doc', doc_name: 'A_DELAYED_KNOWLEDGE_SECRET', title: 'A source', content: 'A_DELAYED_KNOWLEDGE_SECRET', score: 1,
      }] }))
    })
    if (path === '/api/agent/bots/default_assistant') return response({ bot: publicBot })
    if (path === '/api/agent/bots/audit_private_b') return response({ bot: privateBot })
    if (path === '/api/agent/templates') return response({ templates: [] })
    if (path === '/api/agent/config') {
      if (init.method === 'POST') {
        window.__auditSettingsWrites.push('model')
        window.__auditModelPayload = JSON.parse(init.body)
        if (window.__auditModelSaveError) return new Response(JSON.stringify({ detail: 'SYNTHETIC_MODEL_SAVE_ERROR' }), { status: 503, headers: { 'Content-Type': 'application/json' } })
        modelConfig = { ...modelConfig, ...window.__auditModelPayload, api_key: '' }
        return response({ success: true, config: modelConfig })
      }
      if (window.__auditModelLoadError) throw new Error('SYNTHETIC_MODEL_LOAD_ERROR')
      return response({ config: modelConfig, providers: [] })
    }
    if (path === '/api/agent/models') {
      if (window.__auditModelListError) throw new Error('SYNTHETIC_MODEL_LIST_ERROR')
      const result = { models: window.__auditModels ?? ['synthetic', 'synthetic-v2'], source: 'remote' }
      if (window.__auditDelayModels) return new Promise(resolve => { window.__auditResolveModels = () => resolve(response(result)) })
      return response(result)
    }
    if (path === '/api/agent/config/test') {
      window.__auditModelTests = (window.__auditModelTests || 0) + 1
      if (window.__auditModelTestError) throw new Error('SYNTHETIC_TEST_ERROR')
      if (window.__auditDelayTest) return new Promise(resolve => { window.__auditResolveTest = () => resolve(response({ success: true, message: 'SYNTHETIC_LATE_TEST' })) })
      return response({ success: true, message: 'SYNTHETIC_CONNECTION_OK' })
    }
    return response({})
  }
})()
