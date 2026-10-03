// ECharts publishes types only for its barrels; each install module the
// Golden Search charts import directly takes its barrel export's type.
declare module 'echarts/lib/chart/bar/install' {
  export { BarChart as install } from 'echarts/charts';
}
declare module 'echarts/lib/chart/line/install' {
  export { LineChart as install } from 'echarts/charts';
}
declare module 'echarts/lib/chart/scatter/install' {
  export { ScatterChart as install } from 'echarts/charts';
}
declare module 'echarts/lib/component/axisPointer/install' {
  export { AxisPointerComponent as install } from 'echarts/components';
}
declare module 'echarts/lib/component/grid/install' {
  export { GridComponent as install } from 'echarts/components';
}
declare module 'echarts/lib/component/marker/installMarkLine' {
  export { MarkLineComponent as install } from 'echarts/components';
}
declare module 'echarts/lib/component/tooltip/install' {
  export { TooltipComponent as install } from 'echarts/components';
}
declare module 'echarts/lib/renderer/installCanvasRenderer' {
  export { CanvasRenderer as install } from 'echarts/renderers';
}
