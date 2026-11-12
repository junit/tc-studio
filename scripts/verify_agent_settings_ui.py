"""Synthetic-only settings checks, invoked by verify_public_bot_ui.py --capabilities.

The parent runner injects a fetch interceptor before application boot, and owns
and closes a named browser session. No real settings or model calls are used.
"""

from pathlib import Path


def verify_settings_dialog(browser, evaluate, click, wait_for):
    def tab(name):
        browser('find', 'role', 'tab', 'click', '--name', name, '--exact')
        wait_for(f'document.querySelector("[role=tab][aria-selected=true]").textContent === "{name}"')

    def checkbox(name):
        browser('find', 'role', 'checkbox', 'click', '--name', name, '--exact')

    def saved():
        wait_for('document.querySelector("dialog").innerText.includes("配置已保存。")')

    def open_advanced(panel):
        if not evaluate(f'document.querySelector("#{panel} details").open'):
            evaluate(f'document.querySelector("#{panel} summary").scrollIntoView({{block:"center"}}); true')
            browser('click', f'#{panel} summary')
        wait_for(f'document.querySelector("#{panel} details").open')

    def save_enabled():
        return evaluate('!document.querySelector("dialog button[type=submit]").disabled')

    def visible(name):
        return f'document.querySelector("dialog").innerText.includes("{name}")'

    def within_viewport(selector):
        return evaluate(f'''(() => {{
          const r = document.querySelector({selector!r}).getBoundingClientRect();
          return r.width > 0 && r.left >= 0 && r.right <= innerWidth + 1 && r.top >= 0 && r.bottom <= innerHeight + 1;
        }})()''')

    click('配置')
    wait_for('!!document.querySelector("dialog[open] #model-name")')
    assert evaluate('document.querySelector("dialog").getBoundingClientRect().width > 850')
    assert not save_enabled()
    assert evaluate('document.querySelectorAll("dialog button[type=submit]").length') == 1
    assert not evaluate('document.querySelector("dialog").innerText.includes("暂无未保存更改")')
    assert not evaluate('document.querySelector("dialog nav").innerText.includes("能力策略版本")')
    assert not evaluate('[...document.querySelectorAll("dialog button")].some(b => b.textContent === "重新载入")')
    assert evaluate('window.__auditRequests.filter(path => path === "/api/agent/models").length') == 0
    assert within_viewport('dialog footer')
    browser('screenshot', '/tmp/tc-studio-settings-model-desktop.png')

    # Native modal focus cannot tab into the underlying conversation/sidebar.
    browser('focus', 'dialog button[type=submit]') if save_enabled() else browser('focus', '#model-provider')
    for _ in range(14):
        browser('press', 'Tab')
        assert evaluate('document.activeElement === document.body || !!document.activeElement.closest("dialog")')
    browser('focus', '#settings-tab-model')
    browser('press', 'ArrowRight')
    wait_for('document.activeElement.id === "settings-tab-capabilities"')
    browser('press', 'End')
    wait_for('document.activeElement.id === "settings-tab-web"')
    assert evaluate('document.querySelector("input[aria-label=网页读取]").checked') is False
    assert evaluate('document.querySelector("#settings-panel-web details").open') is False
    tab('模型连接')
    assert evaluate('document.querySelector("#settings-panel-model details").open') is False

    # A model-list response from an older URL must not replace current choices.
    evaluate('window.__auditDelayModels = true; true')
    click('获取模型列表')
    wait_for('typeof window.__auditResolveModels === "function"')
    browser('fill', '#model-url', 'http://127.0.0.1:11435')
    evaluate('window.__auditResolveModels(); window.__auditDelayModels = false; true')
    assert evaluate('document.querySelector("#model-name").getAttribute("aria-expanded")') == 'false'
    assert not evaluate('!!document.querySelector("#settings-models")')
    browser('fill', '#model-url', 'http://127.0.0.1:11434')
    click('获取模型列表')
    wait_for('document.querySelectorAll("#settings-models [role=option]").length === 2')
    assert not evaluate('document.querySelector("dialog").innerText.includes("已获取 2 个模型")')
    # Fetch is immediately after the selector; actual pointer selection must work,
    # even if the current configured model is not contained in the fetched list.
    assert evaluate('''(() => {
      const input = document.querySelector('#model-name').getBoundingClientRect();
      const fetch = [...document.querySelectorAll('dialog button')].find(b => b.textContent === '获取模型列表').getBoundingClientRect();
      return input.right <= fetch.left && Math.abs(input.top - fetch.top) < 2;
    })()''')
    browser('fill', '#model-name', 'configured-model-absent-from-list')
    wait_for('document.querySelector("#settings-models").parentElement.innerText.includes("没有匹配模型")')
    browser('press', 'Enter')
    assert evaluate('document.querySelector("#model-name").value') == 'configured-model-absent-from-list'
    assert evaluate('document.querySelector("#model-name").getAttribute("aria-expanded")') == 'false'
    assert evaluate('window.__auditSettingsWrites') == []
    browser('click', '#model-name')
    wait_for('document.querySelectorAll("#settings-models [role=option]").length === 2')
    browser('find', 'role', 'option', 'click', '--name', 'synthetic-v2', '--exact')
    assert evaluate('document.querySelector("#model-name").value') == 'synthetic-v2'
    assert evaluate('document.activeElement.id') == 'model-name'
    browser('press', 'ArrowDown')
    browser('press', 'ArrowDown')
    browser('press', 'ArrowDown')  # Wrap to the first candidate.
    browser('press', 'ArrowUp')  # Wrap back to the last candidate.
    assert evaluate('document.querySelector("#model-name").getAttribute("aria-activedescendant")') == 'model-option-1'
    browser('press', 'Enter')
    assert evaluate('document.querySelector("#model-name").value') == 'synthetic-v2'
    assert evaluate('window.__auditSettingsWrites') == []  # Enter must not submit the form.
    browser('click', '#model-name')
    browser('press', 'Escape')
    assert evaluate('document.querySelector("#model-name").getAttribute("aria-expanded")') == 'false'
    assert not evaluate('document.querySelector("dialog").innerText.includes("放弃未保存的更改？")')
    browser('fill', '#model-name', 'synthetic-v')
    wait_for('document.querySelectorAll("#settings-models [role=option]").length === 1')
    browser('press', 'ArrowDown')
    browser('press', 'Enter')
    assert evaluate('document.querySelector("#model-name").value') == 'synthetic-v2'
    browser('click', '#model-name')
    browser('press', 'Tab')
    assert evaluate('document.querySelector("#model-name").getAttribute("aria-expanded")') == 'false'
    browser('fill', '#model-name', 'synthetic')
    browser('press', 'Escape')
    browser('click', '#model-url')
    evaluate('window.__auditModelTestError = true; true')
    click('测试已保存连接')
    wait_for(visible('SYNTHETIC_TEST_ERROR'))
    evaluate('window.__auditModelTestError = false; true')
    click('测试已保存连接')
    wait_for(visible('SYNTHETIC_CONNECTION_OK'))
    assert evaluate('''(() => {
      const row = document.querySelector('[data-model-test-row]');
      const button = row.querySelector('button').getBoundingClientRect();
      const result = row.querySelector('[data-model-test-result]').getBoundingClientRect();
      return result.left >= button.right && Math.abs((result.top + result.bottom) / 2 - (button.top + button.bottom) / 2) < 2;
    })()''')
    browser('screenshot', '/tmp/tc-studio-settings-model-test-desktop.png')

    browser('fill', '#model-name', 'synthetic-v2')
    unload = evaluate('''(() => {
      const same = new Event('beforeunload', {cancelable:true}); window.dispatchEvent(same);
      const previous = sessionStorage.getItem('tc_studio_access_token');
      sessionStorage.setItem('tc_studio_access_token', 'synthetic-different-identity');
      const changed = new Event('beforeunload', {cancelable:true}); window.dispatchEvent(changed);
      if (previous === null) sessionStorage.removeItem('tc_studio_access_token');
      else sessionStorage.setItem('tc_studio_access_token', previous);
      return {same:same.defaultPrevented, changed:changed.defaultPrevented};
    })()''')
    assert unload == {'same': True, 'changed': False}, unload
    open_advanced('settings-panel-model')
    browser('fill', '#model-temperature', '0')
    assert evaluate('document.querySelector("#model-temperature").value') == '0'
    tab('能力权限')
    checkbox('文件读写与发布')
    wait_for(visible('依赖未启用：文件读写与发布'))
    browser('screenshot', '/tmp/tc-studio-settings-capabilities-desktop.png')
    tab('网页访问')
    assert evaluate('document.querySelector("#model-temperature").value') == '0'
    browser('find', 'role', 'switch', 'click', '--name', '网页读取', '--exact')
    tab('模型连接')  # Validate a hidden invalid section before *any* write.
    click('保存更改')
    wait_for(visible('启用网页读取前必须批准至少一个完整域名'))
    assert evaluate('document.activeElement.id') == 'web-domains'
    assert evaluate('window.__auditSettingsWrites') == []
    browser('fill', '#web-domains', 'pages.example.test\nsub.pages.example.test')
    open_advanced('settings-panel-web')
    browser('fill', '#web-total_timeout_seconds', '31')
    click('保存更改')
    wait_for(visible('整体超时（秒）必须为 2 至 30 的整数'))
    assert evaluate('window.__auditSettingsWrites') == []
    browser('fill', '#web-total_timeout_seconds', '10')
    click('保存更改')
    saved()
    assert evaluate('window.__auditSettingsWrites') == ['policy', 'model']
    assert evaluate('window.__auditModelPayload.temperature') == 0, evaluate('({sent:window.__auditModelPayload.temperature, field:document.querySelector("#model-temperature").value})')
    assert evaluate('Object.hasOwn(window.__auditModelPayload, "api_key")') is False
    payload = evaluate('window.__auditCapabilityPayload')
    assert payload['expected_version'] == 0 and payload['disabled_ids'] == ['files']
    assert payload['enabled_ids'] == ['web_read']
    assert payload['web_read']['allowed_domains'] == ['pages.example.test', 'sub.pages.example.test']
    assert not save_enabled()
    browser('screenshot', '/tmp/tc-studio-settings-web-desktop.png')

    # CAS conflicts stop before the global model write and require explicit reload.
    tab('模型连接')
    browser('fill', '#model-name', 'pending-after-conflict')
    tab('能力权限')
    checkbox('云 API')
    evaluate('window.__auditCapabilityConflict = true; true')
    click('保存更改')
    wait_for(visible('SYNTHETIC_VERSION_CONFLICT'))
    assert evaluate('window.__auditSettingsWrites') == ['policy', 'model', 'policy']
    assert not save_enabled()
    click('重新载入')
    wait_for(visible('重新载入并放弃更改？'))
    click('继续编辑')
    tab('模型连接')
    assert evaluate('document.querySelector("#model-name").value') == 'pending-after-conflict'
    browser('fill', '#model-name', 'still-conflicted')
    assert evaluate('document.querySelector("dialog").innerText.includes("SYNTHETIC_VERSION_CONFLICT")')
    assert not save_enabled()
    evaluate('window.__auditCapabilityConflict = false; true')
    click('重新载入')
    click('放弃并重新载入')
    wait_for('document.querySelector("#model-name")?.value === "synthetic-v2"')

    # Network failure recovers without losing a draft or leaving saving locked.
    tab('能力权限')
    checkbox('云 API')
    evaluate('window.__auditCapabilitySaveError = true; true')
    click('保存更改')
    wait_for(visible('SYNTHETIC_SAVE_NETWORK_ERROR'))
    assert save_enabled()
    assert evaluate('document.querySelector(\'input[aria-label="云 API"]\').checked') is False
    evaluate('window.__auditCapabilitySaveError = false; window.__auditModelSaveError = true; true')
    tab('模型连接')
    browser('fill', '#model-name', 'partial-save-model')
    click('保存更改')
    wait_for(visible('能力与网页访问已保存；模型配置保存失败'))
    before_retry = evaluate('window.__auditSettingsWrites.length')
    assert evaluate('document.querySelector("#model-name").value') == 'partial-save-model'
    assert save_enabled()
    evaluate('window.__auditModelSaveError = false; true')
    click('保存更改')
    saved()
    assert evaluate('window.__auditSettingsWrites.slice(' + str(before_retry) + ')') == ['model']

    # Escape and close protect unsaved edits; discard restores the opener's focus.
    browser('fill', '#model-name', 'discard-this-draft')
    browser('press', 'Escape')
    wait_for(visible('放弃未保存的更改？'))
    click('继续编辑')
    assert evaluate('document.querySelector("#model-name").value') == 'discard-this-draft'
    browser('press', 'Escape')
    click('放弃并关闭')
    wait_for('!document.querySelector("dialog[open]")')
    assert evaluate('document.activeElement.textContent.trim()') == '配置'
    click('配置')
    wait_for('document.querySelector("#model-name")?.value === "partial-save-model"')
    evaluate('window.__auditCapabilityLoadError = true; true')
    click('关闭')
    click('配置')
    wait_for(visible('SYNTHETIC_LOAD_NETWORK_ERROR'))
    assert evaluate('document.querySelector("[data-settings-content]").getAttribute("aria-busy")') == 'false'
    assert not save_enabled()
    evaluate('window.__auditCapabilityLoadError = false; true')
    click('重新载入')
    wait_for('document.querySelector("#model-name")?.value === "partial-save-model"')

    # Reopening is the normal refresh path. Closing cancels every auxiliary
    # request; its late reply cannot populate the reopened form.
    tab('模型连接')
    evaluate('window.__auditDelayModels = true; window.__auditResolveModels = null; true')
    click('获取模型列表')
    wait_for('typeof window.__auditResolveModels === "function"')
    click('关闭')
    click('配置')
    wait_for('document.querySelector("#model-name")?.value === "partial-save-model"')
    evaluate('window.__auditResolveModels(); window.__auditDelayModels = false; true')
    assert not evaluate('!!document.querySelector("#settings-models")')
    evaluate('window.__auditDelayTest = true; true')
    click('测试已保存连接')
    wait_for('typeof window.__auditResolveTest === "function"')
    click('关闭')
    click('配置')
    wait_for('document.querySelector("#model-name")?.value === "partial-save-model"')
    evaluate('window.__auditResolveTest(); window.__auditDelayTest = false; true')
    assert evaluate('document.querySelector("dialog").innerText.includes("SYNTHETIC_LATE_TEST")') is False

    click('关闭')
    mobile_url = evaluate('''(() => {
      const url = new URL(location.href);
      url.searchParams.set('audit-settings-mobile', '1');
      return url.href;
    })()''')
    browser('set', 'viewport', '390', '844')
    # Start a fresh mobile document: verify the settings surface without making
    # its acceptance depend on the unrelated BOT drawer's exit animation. CDP
    # viewport changes after long sessions also do not always emit native resize.
    fixture = Path(__file__).resolve().parents[1] / 'tests/fixtures/public_bot_ui.js'
    browser('--init-script', str(fixture), 'open', mobile_url)
    wait_for('document.body.innerText.includes("A_CURRENT_HISTORY")')
    assert evaluate('window.__auditSettingsWrites') == []
    wait_for('!!document.querySelector("button[title=打开智能体列表]")')
    wait_for('document.querySelector("aside").getBoundingClientRect().right <= 1')
    browser('snapshot', '-i')
    browser('click', 'button[title="打开智能体列表"]')
    browser('screenshot', '/tmp/tc-studio-settings-mobile-menu.png')
    wait_for('''(() => {
      const button = document.querySelector('aside button[aria-haspopup="dialog"]');
      const r = button.getBoundingClientRect();
      return r.left >= 0 && r.right <= innerWidth && button.contains(document.elementFromPoint(r.x + r.width/2, r.y + r.height/2));
    })()''')
    click('配置')
    wait_for('!!document.querySelector("dialog[open] #model-name")')
    assert within_viewport('dialog footer')
    assert within_viewport('#model-name')
    click('获取模型列表')
    wait_for('document.querySelectorAll("#settings-models [role=option]").length === 2')
    assert within_viewport('#settings-models')
    browser('screenshot', '/tmp/tc-studio-settings-model-options-mobile.png')
    browser('find', 'role', 'option', 'click', '--name', 'synthetic-v2', '--exact')
    assert evaluate('document.querySelector("#model-name").value') == 'synthetic-v2'
    # Empty lists and failures must leave manual entry and retries usable.
    evaluate('window.__auditModels = []; true')
    click('获取模型列表')
    wait_for(visible('未获取到可用模型'))
    assert not evaluate('!!document.querySelector("#settings-models")')
    evaluate('window.__auditModelListError = true; true')
    click('获取模型列表')
    wait_for(visible('SYNTHETIC_MODEL_LIST_ERROR'))
    evaluate('window.__auditModelListError = false; window.__auditModels = undefined; true')
    click('获取模型列表')
    wait_for('document.querySelectorAll("#settings-models [role=option]").length === 2')
    browser('fill', '#model-name', 'custom-mobile-model')
    browser('press', 'Escape')
    browser('click', '#model-url')
    browser('screenshot', '/tmp/tc-studio-settings-model-mobile.png')
    for name in ['能力权限', '网页访问']:
        tab(name)
        assert within_viewport('dialog footer')
        assert evaluate('document.querySelector("dialog").scrollWidth <= innerWidth')
    assert within_viewport('#web-domains')
    browser('screenshot', '/tmp/tc-studio-settings-web-mobile.png')
    browser('find', 'role', 'switch', 'click', '--name', '网页读取', '--exact')
    browser('fill', '#web-domains', 'pages.example.test\nmobile.example.test')
    click('保存更改')
    saved()
    assert evaluate('window.__auditCapabilityPayload.web_read.allowed_domains') == ['pages.example.test', 'mobile.example.test']
    assert evaluate('window.__auditCapabilityPayload.enabled_ids') == ['web_read']
    # Run the accessibility tool after interaction checks and scope it to the new
    # dialog, so unrelated application landmarks do not contaminate the result.
    audit = browser('a11y', '--selector', 'dialog', '--tags', 'wcag2a,wcag2aa')
    assert audit.get('counts', {}).get('violations') == 0, audit
    assert not audit.get('violations'), audit
    print(f'Settings accessibility: 0 violations; {audit["counts"]["incomplete"]} incomplete rules require visual review')
    browser('set', 'viewport', '1280', '900')
    tab('模型连接')
    evaluate('document.documentElement.classList.add("dark"); true')
    browser('screenshot', '/tmp/tc-studio-settings-model-desktop-dark.png')
    dark_audit = browser('a11y', '--selector', 'dialog', '--tags', 'wcag2a,wcag2aa')
    assert dark_audit.get('counts', {}).get('violations') == 0, dark_audit
    print(f'Dark settings accessibility: 0 violations; {dark_audit["counts"]["incomplete"]} incomplete rules require visual review')
    click('关闭')
    print('PASS: model dropdown pointer/keyboard/search/manual entry, empty/error retry, adjacent fetch, stale responses, unified save, CAS conflict, partial retry, dirty close, desktop/mobile, scoped accessibility (synthetic API only)')
