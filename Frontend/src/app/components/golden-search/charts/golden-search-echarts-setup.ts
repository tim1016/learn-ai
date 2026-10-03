import { install as BarChart } from 'echarts/lib/chart/bar/install';
import { install as CustomChart } from 'echarts/lib/chart/custom/install';
import { install as HeatmapChart } from 'echarts/lib/chart/heatmap/install';
import { install as LineChart } from 'echarts/lib/chart/line/install';
import { install as ScatterChart } from 'echarts/lib/chart/scatter/install';
import { install as AxisPointerComponent } from 'echarts/lib/component/axisPointer/install';
import { install as GridComponent } from 'echarts/lib/component/grid/install';
import { install as MarkLineComponent } from 'echarts/lib/component/marker/installMarkLine';
import { install as TooltipComponent } from 'echarts/lib/component/tooltip/install';
import { install as VisualMapComponent } from 'echarts/lib/component/visualMap/installVisualMapContinuous';
import { install as CanvasRenderer } from 'echarts/lib/renderer/installCanvasRenderer';
import { init, use } from 'echarts/core';

// Each feature comes from its own install module, not the `echarts/charts`
// and `echarts/components` barrels: through a barrel the build keeps every
// chart type and component, the whole library. Loaded only through
// `loadGoldenSearchCharts`, so ECharts stays out of the initial bundle.
use([LineChart, BarChart, ScatterChart, HeatmapChart, CustomChart, GridComponent, TooltipComponent, AxisPointerComponent, MarkLineComponent, VisualMapComponent, CanvasRenderer]);

export { init };
