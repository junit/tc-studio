"""Chromium regression for BOT switching, using synthetic API responses only.

Requires agent-browser and a running TC Studio frontend. It never sends API
requests to the running backend and never creates/deletes real user data.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:8000')
    parser.add_argument('--sidebar', action='store_true', help='Check running state and description replace the last message')
    parser.add_argument('--capabilities', action='store_true', help='Check scoped capability settings and title click layout')
    parser.add_argument('--stream-case', choices=['error', 'split-error', 'sync-error', 'truncated', 'done'], help='Check SSE terminal errors and split event framing')
    options = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    session = f'tc-public-ui-{os.getpid()}'
    command = ['rtk', 'proxy', 'agent-browser', '--session', session, '--json']

    def browser(*arguments: str):
        process = subprocess.run(command + list(arguments), capture_output=True, text=True, timeout=45, cwd=root)
        if process.returncode:
            raise RuntimeError(process.stdout + process.stderr)
        result = json.loads(process.stdout)
        if not result.get('success'):
            raise RuntimeError(str(result.get('error')))
        return result.get('data', {})

    def evaluate(code: str):
        return browser('eval', code).get('result')

    def click(name: str):
        browser('find', 'role', 'button', 'click', '--name', name, '--exact')

    def wait_for(code: str):
        browser('wait', '--fn', code)

    def close_settings():
        click('关闭设置')
        wait_for('!document.querySelector("button[title=关闭设置]")')

    def open_settings():
        click('设置')
        wait_for('[...document.querySelectorAll("aside")].some(el => el.querySelector("button[title=关闭设置]") && el.getBoundingClientRect().width > 300 && el.getBoundingClientRect().right <= innerWidth + 1)')

    try:
        suffix = '/?audit-sidebar=1#agent' if options.sidebar else '/#agent'
        if options.stream_case:
            suffix = f'/?audit-stream={options.stream_case}#agent'
        if options.capabilities:
            suffix = '/?audit-capabilities=1#agent'
        browser('--init-script', str(root / 'tests/fixtures/public_bot_ui.js'), 'open', options.url.rstrip('/') + suffix)
        wait_for('document.body.innerText.includes("A_CURRENT_HISTORY")')
        if options.capabilities:
            title = evaluate('''(() => {
              const title = [...document.querySelectorAll('h2')].find(el => el.textContent === 'AUDIT_PUBLIC_A');
              return {icons:title.querySelectorAll('svg').length, flex:getComputedStyle(title).display,
                      width:title.getBoundingClientRect().width, textWidth:title.firstElementChild.getBoundingClientRect().width};
            })()''')
            assert title['icons'] == 0 and title['flex'] != 'flex', title
            assert abs(title['width'] - title['textWidth']) < 2, title
            browser('find', 'role', 'heading', 'click', '--name', 'AUDIT_PUBLIC_A', '--exact')
            wait_for('[...document.querySelectorAll("aside")].some(el => el.querySelector("button[title=关闭设置]") && el.getBoundingClientRect().width > 300 && el.getBoundingClientRect().right <= innerWidth + 1)')
            close_settings()
            from verify_agent_settings_ui import verify_settings_dialog
            verify_settings_dialog(browser, evaluate, click, wait_for)
            return
        if options.stream_case:
            browser('fill', 'textarea[placeholder^="给 AUDIT_PUBLIC_A"]', 'synthetic stream check')
            browser('press', 'Enter')
            wait_for('window.__auditStreamFinished && ![...document.querySelectorAll("button")].some(b => b.textContent.trim() === "停止")')
            result = evaluate('''(() => {
              const text = document.body.innerText;
              return {errorCount: text.split('SYNTHETIC_MODEL_DEPENDENCY_ERROR').length - 1,
                      interrupted: text.includes('连接意外中断'), unknown: text.includes('未知异常'),
                      partial: text.includes('SYNTHETIC_PARTIAL_ANSWER'), complete: text.includes('SYNTHETIC_COMPLETE_ANSWER')};
            })()''')
            if options.stream_case in ('error', 'split-error', 'sync-error'):
                assert result['errorCount'] == 1 and not result['interrupted'] and not result['unknown'], result
            elif options.stream_case == 'truncated':
                assert result['partial'] and result['interrupted'], result
            else:
                assert result['complete'] and not result['interrupted'], result
            print(f'PASS: SSE {options.stream_case}; accurate terminal state and cleared generating state (synthetic API fixture)')
            return
        if options.sidebar:
            def inspect_sidebar():
                return evaluate('''(() => {
                  const button = [...document.querySelectorAll('aside button')].find(b => b.innerText.includes('AUDIT_PRIVATE_B'));
                  const status = button?.querySelector('[role="status"]');
                  const row = status?.parentElement;
                  const title = [...(button?.querySelectorAll('span') || [])].find(s => s.textContent === 'AUDIT_PRIVATE_B');
                  return {status:status?.textContent, row:row?.textContent, titleRow:title?.parentElement?.parentElement?.textContent,
                          statusFirst:row?.firstElementChild === status};
                })()''')

            wait_for('document.body.innerText.includes("思考中")')
            result = inspect_sidebar()
            assert result['statusFirst'] and '我：正在分析问题……' in result['row'], result
            assert 'SIDEBAR_LAST_MESSAGE' not in result['row'], result
            assert '思考中' not in result['titleRow'], result
            stages = [
                ('coding', '编写中', '正在编写执行代码……'),
                ('executing', '执行中 #3', '正在执行 Python / 工具代码……'),
                ('waiting_approval', '待确认', '等待确认操作……'),
                ('answering', '回复中', '正在组织并输出回答……'),
            ]
            for stage, label, description in stages:
                evaluate(f'window.__auditRunStatus = {json.dumps(stage)}; true')
                wait_for(f'document.body.innerText.includes({json.dumps(label)})')
                result = inspect_sidebar()
                assert result['statusFirst'] and f'我：{description}' in result['row'], result
                assert 'SIDEBAR_LAST_MESSAGE' not in result['row'] and label not in result['titleRow'], result
            evaluate('window.__auditRunStatus = null; true')
            wait_for('document.body.innerText.includes("SIDEBAR_LAST_MESSAGE")')
            assert evaluate('![...document.querySelectorAll("aside button")].find(b => b.innerText.includes("AUDIT_PRIVATE_B"))?.querySelector("[role=status]")')
            print('PASS: all five running stages replace the last message; completion restores it; BOT name stays clear (synthetic API fixture)')
            return
        open_settings()
        click('知识库 (0)')
        browser('wait', 'input[placeholder^="输入问题或关键词测试召回"]')
        browser('fill', 'input[placeholder^="输入问题或关键词测试召回"]', 'isolate')
        click('测试召回')
        wait_for('typeof window.__auditPendingSearch === "function"')
        close_settings()
        click('🤖 AUDIT_PRIVATE_B 通用 Synthetic isolation audit fixture')
        wait_for('document.body.innerText.includes("B_CURRENT_HISTORY")')
        evaluate('window.__auditPendingSearch(); true')
        open_settings()
        result = evaluate('({leaked:document.body.innerText.includes("A_DELAYED_KNOWLEDGE_SECRET"), staleQuery:[...document.querySelectorAll("input")].some(i=>i.value==="isolate")})')
        assert result == {'leaked': False, 'staleQuery': False}, result
        close_settings()

        click('🤖 内置 AUDIT_PUBLIC_A 通用 Synthetic isolation audit fixture')
        wait_for('document.body.innerText.includes("A_CURRENT_HISTORY")')
        click('向上滑动或点击加载更早历史')
        wait_for('typeof window.__auditPendingHistory === "function"')
        click('🤖 AUDIT_PRIVATE_B 通用 Synthetic isolation audit fixture')
        wait_for('document.body.innerText.includes("B_CURRENT_HISTORY")')
        evaluate('window.__auditPendingHistory(); true')
        assert evaluate('document.body.innerText.includes("A_DELAYED_HISTORY_SECRET")') is False

        boots = evaluate('Number(sessionStorage.getItem("__auditBootCount"))')
        evaluate('sessionStorage.setItem("tc_studio_access_token", "synthetic-account-b"); true')
        wait_for(f'Number(sessionStorage.getItem("__auditBootCount")) > {boots}')
        print('PASS: delayed knowledge, delayed history, search reset, identity-change reload (synthetic API fixture)')
    except Exception:
        if options.capabilities:
            print(evaluate('''(() => {
              const heading = document.querySelector('h2');
              const rect = heading?.getBoundingClientRect();
              return {viewport:[innerWidth,innerHeight,devicePixelRatio], rect:rect?.toJSON(),
                      hit:rect ? document.elementFromPoint(rect.x + rect.width/2, rect.y + rect.height/2)?.outerHTML : null};
            })()'''))
        browser('screenshot', '/tmp/tc-studio-ui-check-failure.png')
        print(browser('snapshot', '-i').get('snapshot', ''))
        raise
    finally:
        browser('close')


if __name__ == '__main__':
    main()
