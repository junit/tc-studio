import React, { useEffect, useRef, useState, useMemo } from 'react'
import * as echarts from 'echarts'
import {
  BarChart3,
  LineChart,
  PieChart,
  Download,
  Code2,
  Copy,
  Check,
  RefreshCw,
  AlertCircle
} from 'lucide-react'

interface EChartsViewerProps {
  code: string
  className?: string
}

// ECharts HTML tooltips assign literal formatter/content strings to innerHTML.
// Chart JSON is model/user controlled, so remove every custom HTML-producing
// hook before handing the option to ECharts.
const stripUnsafeEchartsContent = (value: any): any => {
  if (Array.isArray(value)) return value.map(stripUnsafeEchartsContent)
  if (!value || typeof value !== 'object') return value
  const clean: Record<string, any> = {}
  for (const [key, child] of Object.entries(value)) {
    if (key === 'formatter' || key === 'content') continue
    clean[key] = stripUnsafeEchartsContent(child)
  }
  return clean
}

export const EChartsViewer: React.FC<EChartsViewerProps> = ({ code, className = '' }) => {
  const chartRef = useRef<HTMLDivElement>(null)
  const chartInstance = useRef<echarts.ECharts | null>(null)
  const [viewMode, setViewMode] = useState<'chart' | 'json'>('chart')
  const [hasCopied, setHasCopied] = useState<boolean>(false)
  const [parseError, setParseError] = useState<string | null>(null)
  const [chartTypeOverride, setChartTypeOverride] = useState<'default' | 'line' | 'bar' | 'pie'>('default')

  // 解析并标准化 ECharts 配置选项
  const { option, isSimple, rawTitle } = useMemo(() => {
    try {
      const trimmed = code.trim()
      const parsed = JSON.parse(trimmed)
      let title = ''

      // 提取标题
      if (typeof parsed.title === 'string') {
        title = parsed.title
      } else if (parsed.title?.text) {
        title = parsed.title.text
      }

      // 如果已经是标准规范的 ECharts 完整配置
      if (parsed.series && (parsed.xAxis || parsed.radar || parsed.geo || parsed.series[0]?.type === 'pie')) {
        let opt = stripUnsafeEchartsContent(parsed)
        if (typeof opt.title === 'string') {
          opt.title = { text: opt.title, left: 'center' }
        }
        return { option: opt, isSimple: false, rawTitle: title }
      }

      // 兼容极简或灵活数据格式
      // 例如: { type: "bar", title: "活跃统计", labels: ["周一", "周二"], data: [100, 200] }
      // 或: { type: "pie", title: "占比", data: [{ name: "A", value: 30 }, { name: "B", value: 70 }] }
      const chartType = parsed.type || 'bar'
      const labels = parsed.labels || parsed.xAxis || parsed.categories || []
      const data = parsed.data || []

      let normalizedSeries: any[] = []
      let normalizedXAxis: any = null
      let normalizedYAxis: any = null

      if (chartType === 'pie') {
        let pieData = Array.isArray(data) ? data : []
        if (labels.length > 0 && Array.isArray(data) && typeof data[0] === 'number') {
          pieData = labels.map((l: string, i: number) => ({ name: l, value: data[i] ?? 0 }))
        }
        normalizedSeries = [
          {
            name: title || '数据占比',
            type: 'pie',
            radius: ['35%', '65%'],
            avoidLabelOverlap: true,
            itemStyle: { borderRadius: 6, borderColor: '#fff', borderWidth: 2 },
            label: { show: true, formatter: '{b}: {d}%' },
            data: pieData,
          },
        ]
      } else {
        // 折线图或柱状图
        normalizedXAxis = {
          type: 'category',
          data: labels,
          axisLine: { lineStyle: { color: '#94a3b8' } },
        }
        normalizedYAxis = {
          type: 'value',
          splitLine: { lineStyle: { type: 'dashed', color: '#e2e8f0' } },
        }

        if (Array.isArray(parsed.series)) {
          normalizedSeries = parsed.series.map((s: any) => ({
            type: chartType,
            smooth: chartType === 'line',
            ...s,
          }))
        } else {
          normalizedSeries = [
            {
              name: title || '数值',
              type: chartType,
              data: data,
              smooth: chartType === 'line',
              itemStyle: { borderRadius: chartType === 'bar' ? [4, 4, 0, 0] : 0 },
            },
          ]
        }
      }

      const generatedOption: echarts.EChartsOption = {
        title: title ? { text: title, left: 'center', textStyle: { fontSize: 13, fontWeight: 'bold' } } : undefined,
        tooltip: { trigger: chartType === 'pie' ? 'item' : 'axis' },
        grid: chartType === 'pie' ? undefined : { left: '3%', right: '4%', bottom: '3%', containLabel: true },
        xAxis: normalizedXAxis,
        yAxis: normalizedYAxis,
        series: normalizedSeries,
      }

      return { option: generatedOption, isSimple: true, rawTitle: title }
    } catch (e: any) {
      return { option: null, isSimple: false, rawTitle: '' }
    }
  }, [code])

  // 渲染并随窗口尺寸与深浅色模式自动更新
  useEffect(() => {
    if (!chartRef.current || !option) return

    setParseError(null)
    const isDark = document.documentElement.classList.contains('dark')

    // 复制配置并根据图表类型切换与主题调整
    const finalOption = stripUnsafeEchartsContent(JSON.parse(JSON.stringify(option)))
    if (Array.isArray(finalOption.tooltip)) {
      finalOption.tooltip = finalOption.tooltip.map((tooltip: any) => ({
        ...(tooltip || {}),
        renderMode: 'richText',
      }))
    } else {
      finalOption.tooltip = {
        ...(finalOption.tooltip || {}),
        renderMode: 'richText',
      }
    }

    // 深色模式文字与网格线调优
    if (isDark) {
      if (finalOption.title?.textStyle) {
        finalOption.title.textStyle.color = '#f1f5f9'
      }
      if (finalOption.xAxis) {
        const xa = Array.isArray(finalOption.xAxis) ? finalOption.xAxis[0] : finalOption.xAxis
        if (xa) {
          xa.axisLabel = { ...(xa.axisLabel || {}), color: '#94a3b8' }
          xa.axisLine = { lineStyle: { color: '#475569' } }
        }
      }
      if (finalOption.yAxis) {
        const ya = Array.isArray(finalOption.yAxis) ? finalOption.yAxis[0] : finalOption.yAxis
        if (ya) {
          ya.axisLabel = { ...(ya.axisLabel || {}), color: '#94a3b8' }
          ya.splitLine = { lineStyle: { type: 'dashed', color: '#334155' } }
        }
      }
      if (finalOption.legend?.textStyle) {
        finalOption.legend.textStyle.color = '#cbd5e1'
      }
    }

    // 支持快捷覆盖图表类型 (仅对非雷达图/地图有效)
    if (chartTypeOverride !== 'default' && finalOption.series && Array.isArray(finalOption.series)) {
      if (chartTypeOverride === 'pie') {
        const firstSeries = finalOption.series[0]
        const dataArr = firstSeries?.data || []
        const categories = (Array.isArray(finalOption.xAxis) ? finalOption.xAxis[0] : finalOption.xAxis)?.data || []
        
        let pieItems = dataArr
        if (categories.length > 0 && typeof dataArr[0] === 'number') {
          pieItems = categories.map((cat: string, idx: number) => ({ name: cat, value: dataArr[idx] }))
        }
        finalOption.xAxis = undefined
        finalOption.yAxis = undefined
        finalOption.grid = undefined
        finalOption.series = [
          {
            type: 'pie',
            radius: ['35%', '65%'],
            data: pieItems,
            itemStyle: { borderRadius: 6, borderColor: isDark ? '#0f172a' : '#ffffff', borderWidth: 2 },
            label: { show: true, formatter: '{b}: {d}%' }
          }
        ]
      } else {
        finalOption.series = finalOption.series.map((s: any) => ({
          ...s,
          type: chartTypeOverride,
          smooth: chartTypeOverride === 'line',
        }))
      }
    }

    // 配色方案（商务深蓝、碧绿、琥珀金、紫罗兰、珊瑚粉）
    if (!finalOption.color) {
      finalOption.color = ['#3b82f6', '#10b981', '#f59e0b', '#8b5cf6', '#ec4899', '#06b6d4', '#6366f1']
    }

    if (!chartInstance.current) {
      chartInstance.current = echarts.init(chartRef.current, isDark ? 'dark' : undefined, {
        renderer: 'canvas',
      })
    }

    try {
      chartInstance.current.setOption(finalOption, true)
    } catch (err: any) {
      setParseError(err.message || 'ECharts 配置渲染失败')
    }

    // 响应式监听容器尺寸变化
    const resizeObserver = new ResizeObserver(() => {
      chartInstance.current?.resize()
    })
    resizeObserver.observe(chartRef.current)

    return () => {
      resizeObserver.disconnect()
    }
  }, [option, chartTypeOverride])

  // 组件卸载时释放图表实例
  useEffect(() => {
    return () => {
      chartInstance.current?.dispose()
      chartInstance.current = null
    }
  }, [])

  // 导出图片
  const handleExportImage = () => {
    if (!chartInstance.current) return
    const url = chartInstance.current.getDataURL({
      type: 'png',
      pixelRatio: 2,
      backgroundColor: document.documentElement.classList.contains('dark') ? '#0f172a' : '#ffffff',
    })
    const a = document.createElement('a')
    a.href = url
    a.download = `${rawTitle || 'chart'}_${Date.now()}.png`
    a.click()
  }

  // 复制代码
  const handleCopyCode = async () => {
    try {
      await navigator.clipboard.writeText(code)
      setHasCopied(true)
      setTimeout(() => setHasCopied(false), 2000)
    } catch (_) {}
  }

  return (
    <div className={`my-3 rounded-2xl border border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900 shadow-xs overflow-hidden not-prose ${className}`}>
      {/* 顶部交互操作栏 */}
      <div className="flex items-center justify-between px-3.5 py-2 border-b border-slate-100 dark:border-slate-800/80 bg-slate-50/80 dark:bg-slate-950/50 text-xs">
        <div className="flex items-center gap-2 min-w-0">
          <div className="w-2 h-2 rounded-full bg-blue-500 animate-pulse shrink-0" />
          <span className="font-semibold text-slate-700 dark:text-slate-300 truncate">
            {rawTitle || '数据可视化图表 (ECharts)'}
          </span>
          <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-blue-50 dark:bg-blue-950/80 border border-blue-200 dark:border-blue-800 text-blue-600 dark:text-blue-400 font-mono">
            交互式图表
          </span>
        </div>

        <div className="flex items-center gap-1.5 shrink-0">
          {/* 切换折线/柱状/饼图 */}
          {viewMode === 'chart' && (
            <div className="flex items-center rounded-lg border border-slate-200 dark:border-slate-800 p-0.5 bg-white dark:bg-slate-900">
              <button
                type="button"
                onClick={() => setChartTypeOverride(chartTypeOverride === 'bar' ? 'default' : 'bar')}
                className={`p-1 rounded-md transition-colors cursor-pointer ${
                  chartTypeOverride === 'bar'
                    ? 'bg-blue-100 text-blue-600 dark:bg-blue-950 dark:text-blue-400'
                    : 'text-slate-400 hover:text-slate-600 dark:hover:text-slate-300'
                }`}
                title="柱状图"
              >
                <BarChart3 className="w-3.5 h-3.5" />
              </button>
              <button
                type="button"
                onClick={() => setChartTypeOverride(chartTypeOverride === 'line' ? 'default' : 'line')}
                className={`p-1 rounded-md transition-colors cursor-pointer ${
                  chartTypeOverride === 'line'
                    ? 'bg-blue-100 text-blue-600 dark:bg-blue-950 dark:text-blue-400'
                    : 'text-slate-400 hover:text-slate-600 dark:hover:text-slate-300'
                }`}
                title="折线图"
              >
                <LineChart className="w-3.5 h-3.5" />
              </button>
              <button
                type="button"
                onClick={() => setChartTypeOverride(chartTypeOverride === 'pie' ? 'default' : 'pie')}
                className={`p-1 rounded-md transition-colors cursor-pointer ${
                  chartTypeOverride === 'pie'
                    ? 'bg-blue-100 text-blue-600 dark:bg-blue-950 dark:text-blue-400'
                    : 'text-slate-400 hover:text-slate-600 dark:hover:text-slate-300'
                }`}
                title="饼图"
              >
                <PieChart className="w-3.5 h-3.5" />
              </button>
            </div>
          )}

          {/* 导出图片 */}
          {viewMode === 'chart' && (
            <button
              type="button"
              onClick={handleExportImage}
              className="p-1 rounded-lg text-slate-400 hover:text-blue-600 hover:bg-slate-100 dark:hover:bg-slate-800 transition-colors cursor-pointer"
              title="导出高清图表为 PNG"
            >
              <Download className="w-3.5 h-3.5" />
            </button>
          )}

          {/* 复制代码 */}
          <button
            type="button"
            onClick={handleCopyCode}
            className="p-1 rounded-lg text-slate-400 hover:text-blue-600 hover:bg-slate-100 dark:hover:bg-slate-800 transition-colors cursor-pointer"
            title="复制图表 JSON 配置"
          >
            {hasCopied ? <Check className="w-3.5 h-3.5 text-emerald-500" /> : <Copy className="w-3.5 h-3.5" />}
          </button>

          {/* 视图切换 (图表 / JSON) */}
          <button
            type="button"
            onClick={() => setViewMode(viewMode === 'chart' ? 'json' : 'chart')}
            className={`px-2 py-0.5 rounded-lg border text-[11px] font-medium flex items-center gap-1 transition-colors cursor-pointer ${
              viewMode === 'json'
                ? 'bg-blue-600 text-white border-blue-600'
                : 'border-slate-200 dark:border-slate-800 text-slate-600 dark:text-slate-400 hover:bg-slate-100 dark:hover:bg-slate-800'
            }`}
          >
            <Code2 className="w-3 h-3" />
            <span>{viewMode === 'chart' ? '配置' : '图表'}</span>
          </button>
        </div>
      </div>

      {/* 主体渲染区 */}
      <div className="relative min-h-[300px] w-full p-2 bg-white dark:bg-slate-900">
        {parseError && (
          <div className="absolute inset-0 z-10 flex flex-col items-center justify-center p-4 bg-white/90 dark:bg-slate-900/90 text-rose-500 text-xs gap-1.5">
            <AlertCircle className="w-5 h-5 text-rose-500" />
            <span className="font-semibold">图表配置解析错误</span>
            <span className="text-[11px] text-slate-400 font-mono text-center max-w-md">{parseError}</span>
          </div>
        )}

        {viewMode === 'chart' ? (
          <div ref={chartRef} className="w-full h-[320px]" />
        ) : (
          <div className="p-3 bg-slate-950 text-emerald-400 font-mono text-[11px] rounded-xl overflow-x-auto max-h-[320px] select-text">
            <pre className="whitespace-pre-wrap">{JSON.stringify(option || code, null, 2)}</pre>
          </div>
        )}
      </div>
    </div>
  )
}
