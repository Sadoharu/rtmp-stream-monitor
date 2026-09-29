import { useEffect, useMemo, useRef } from "react";
import * as echarts from "echarts/core";
import { CustomChart, LineChart } from "echarts/charts";
import { DataZoomComponent, GridComponent, MarkAreaComponent, TooltipComponent } from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";
import type { CustomSeriesRenderItemAPI, CustomSeriesRenderItemParams, EChartsOption } from "echarts";
import type { EventsResponse, SeriesResponse, TimelineEvent } from "../api";
import { eventLabel, formatDateTime, formatMbps, formatTime } from "../format";

type Props = {
  series: SeriesResponse | null;
  events: EventsResponse | null;
  from: number;
  to: number;
  visibleProbeIds: string[];
  onToggleProbe: (probeId: string) => void;
  onSelectEvent: (event: TimelineEvent) => void;
};

const COLORS = ["#43d9b8", "#68a8ff", "#ffb86b", "#c69cff", "#ff7782", "#8ed08a", "#f1d06a", "#70d0df"];
const SEVERITY_COLOR: Record<string, string> = { CRITICAL: "#ff6670", WARNING: "#ffbd5b", INFO: "#6baeff" };
echarts.use([LineChart, CustomChart, DataZoomComponent, GridComponent, MarkAreaComponent, TooltipComponent, CanvasRenderer]);

function escapeHtml(value: unknown): string {
  return String(value ?? "").replace(/[&<>"']/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[character] ?? character);
}

function probeColor(index: number): string {
  return COLORS[index % COLORS.length];
}

function eventTooltip(event: TimelineEvent): string {
  return `<div class="chart-tooltip-title">${escapeHtml(eventLabel(event))}</div><div>${escapeHtml(event.probe_name ?? "Інцидент")}</div><div class="chart-tooltip-muted">${escapeHtml(formatDateTime(event.started_at))} · ${escapeHtml(event.severity)}</div>`;
}

export function StreamChart({ series, events, from, to, visibleProbeIds, onToggleProbe, onSelectEvent }: Props) {
  const hostRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<echarts.EChartsType | null>(null);
  const seriesRows = series?.series ?? [];
  const eventRows = events?.events ?? [];
  const seriesRowsRef = useRef(seriesRows);
  seriesRowsRef.current = seriesRows;
  const eventRowsRef = useRef(eventRows);
  eventRowsRef.current = eventRows;
  const visible = useMemo(() => new Set(visibleProbeIds), [visibleProbeIds]);

  useEffect(() => {
    if (!hostRef.current) return;
    const chart = echarts.init(hostRef.current, undefined, { renderer: "canvas" });
    chartRef.current = chart;
    const resize = new ResizeObserver(() => chart.resize());
    resize.observe(hostRef.current);
    const onCanvasClick = (rawEvent: unknown) => {
      const pointer = rawEvent as { offsetX?: number; offsetY?: number };
      if (typeof pointer.offsetX !== "number" || typeof pointer.offsetY !== "number") return;
      const rows = seriesRowsRef.current;
      const events = eventRowsRef.current;
      const hasGlobalEvents = events.some((event) => !event.probe_id);
      const lanes = [...(hasGlobalEvents ? ["Інцидент"] : []), ...rows.map((row) => row.probe.name)];
      let nearest: { event: TimelineEvent; distanceSquared: number } | null = null;
      for (const event of events) {
        const probeIndex = rows.findIndex((row) => row.probe.id === event.probe_id);
        const lane = !event.probe_id ? 0 : probeIndex < 0
          ? Math.max(0, lanes.indexOf(event.probe_name ?? ""))
          : probeIndex + (hasGlobalEvents ? 1 : 0);
        const start = Date.parse(event.started_at);
        const end = event.ended_at ? Date.parse(event.ended_at) : event.state === "ACTIVE" ? to : start;
        const startPoint = chart.convertToPixel({ xAxisIndex: 1, yAxisIndex: 1 }, [start, lane]) as number[];
        const endPoint = chart.convertToPixel({ xAxisIndex: 1, yAxisIndex: 1 }, [Math.max(start, end), lane]) as number[];
        if (!Number.isFinite(startPoint?.[0]) || !Number.isFinite(startPoint?.[1]) || !Number.isFinite(endPoint?.[0])) continue;
        const left = Math.min(startPoint[0], endPoint[0]);
        const right = Math.max(startPoint[0], endPoint[0]);
        const nearestX = Math.max(left, Math.min(right, pointer.offsetX));
        const dx = pointer.offsetX - nearestX;
        const dy = pointer.offsetY - startPoint[1];
        const distanceSquared = dx * dx + dy * dy;
        if (!nearest || distanceSquared < nearest.distanceSquared) nearest = { event, distanceSquared };
      }
      if (nearest && nearest.distanceSquared <= 14 * 14) onSelectEvent(nearest.event);
    };
    chart.getZr().on("click", onCanvasClick);
    return () => {
      resize.disconnect();
      chart.getZr().off("click", onCanvasClick);
      chart.dispose();
      chartRef.current = null;
    };
  }, [onSelectEvent]);

  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;

    const showDateTicks = to - from >= 2 * 24 * 60 * 60 * 1000;
    const probeNames = seriesRows.map((row) => row.probe.name);
    const hasGlobalEvents = eventRows.some((event) => !event.probe_id);
    const lanes = [...(hasGlobalEvents ? ["Інцидент"] : []), ...probeNames];
    if (!lanes.length) lanes.push("Події");
    const laneForEvent = (event: TimelineEvent) => {
      if (!event.probe_id) return 0;
      const probeIndex = seriesRows.findIndex((row) => row.probe.id === event.probe_id);
      return probeIndex < 0 ? Math.max(0, lanes.indexOf(event.probe_name ?? "")) : probeIndex + (hasGlobalEvents ? 1 : 0);
    };

    const visibleRows = seriesRows.filter((row) => visible.has(row.probe.id));
    const rangeSeries = visibleRows.map((row) => {
      const index = seriesRows.findIndex((candidate) => candidate.probe.id === row.probe.id);
      const color = probeColor(index);
      const data = row.points
        .filter((point) => Number.isFinite(point.min_bps) && Number.isFinite(point.max_bps))
        .map((point) => ({ value: [Date.parse(point.timestamp), point.min_bps / 1_000_000, point.max_bps / 1_000_000, point.bucket_seconds] }));
      return {
        id: `${row.probe.id}:range`,
        name: `${row.probe.name} min/max`,
        type: "custom" as const,
        xAxisIndex: 0,
        yAxisIndex: 0,
        clip: true,
        silent: true,
        z: 1,
        tooltip: { show: false },
        data,
        renderItem: (_params: CustomSeriesRenderItemParams, api: CustomSeriesRenderItemAPI) => {
          const timestamp = Number(api.value(0));
          const min = Number(api.value(1));
          const max = Number(api.value(2));
          const bucketSeconds = Number(api.value(3));
          const lower = api.coord([timestamp, min]);
          const upper = api.coord([timestamp, max]);
          const bucketSize = api.size?.([bucketSeconds * 1000, 0]) ?? 0;
          const bucketWidth = Math.abs(typeof bucketSize === "number" ? bucketSize : bucketSize[0] ?? 0);
          const width = Math.max(2, Math.min(24, bucketWidth * 0.72));
          return {
            type: "rect",
            shape: { x: lower[0] - width / 2, y: Math.min(lower[1], upper[1]), width, height: Math.max(1, Math.abs(lower[1] - upper[1])) },
            style: { fill: color, opacity: 0.3 },
          };
        },
      };
    });

    const lineSeries = visibleRows
      .map((row) => {
        const index = seriesRows.findIndex((candidate) => candidate.probe.id === row.probe.id);
        const data: Array<Record<string, unknown>> = row.points.map((point) => ({
          value: [Date.parse(point.timestamp), point.avg_bps / 1_000_000],
          meta: point,
        }));
        for (const gap of row.gaps) {
          data.push({ value: [Date.parse(gap.from), null], gap });
          data.push({ value: [Date.parse(gap.to), null], gap });
        }
        data.sort((left, right) => Number((left.value as unknown[])[0]) - Number((right.value as unknown[])[0]));
        const color = probeColor(index);
        return {
          id: row.probe.id,
          name: row.probe.name,
          type: "line" as const,
          z: 2,
          xAxisIndex: 0,
          yAxisIndex: 0,
          showSymbol: false,
          connectNulls: false,
          sampling: "lttb" as const,
          data,
          lineStyle: { width: 2.4, color },
          itemStyle: { color },
          emphasis: { focus: "series" as const, lineStyle: { width: 3.5 } },
          markArea: {
            silent: true,
            label: { show: false },
            itemStyle: { color: `${color}13` },
            data: row.gaps.map((gap) => [
              { xAxis: Date.parse(gap.from), name: gap.reason },
              { xAxis: Date.parse(gap.to) },
            ]),
          },
        };
      });

    const eventData = eventRows.map((event) => {
      const start = Date.parse(event.started_at);
      const end = event.ended_at ? Date.parse(event.ended_at) : event.state === "ACTIVE" ? to : start;
      return {
        name: eventLabel(event),
        value: [start, Math.max(start, end), laneForEvent(event)],
        event,
        itemStyle: { color: SEVERITY_COLOR[event.severity] ?? "#78a7d4" },
      };
    });

    const option: EChartsOption = {
      animation: false,
      backgroundColor: "transparent",
      grid: [
        { left: 75, right: 25, top: 34, height: "47%", containLabel: false },
        { left: 75, right: 25, top: "64%", height: "22%", containLabel: false },
      ],
      axisPointer: { link: [{ xAxisIndex: [0, 1] }], label: { backgroundColor: "#24333e" } },
      tooltip: {
        trigger: "axis",
        confine: true,
        backgroundColor: "#111a21",
        borderColor: "#31434e",
        textStyle: { color: "#e8f1f4", fontSize: 12 },
        axisPointer: { type: "line", lineStyle: { color: "#8aa0ae", type: "dashed" } },
        formatter: (rawParams) => {
          const params = Array.isArray(rawParams) ? rawParams : [rawParams];
          const eventParam = params.find((parameter) => parameter.seriesId === "event-timeline");
          const event = (eventParam?.data as { event?: TimelineEvent } | undefined)?.event;
          if (event) return eventTooltip(event);
          const at = (params[0] as { axisValue?: unknown } | undefined)?.axisValue;
          const details = params.filter((parameter) => parameter.seriesType === "line").map((parameter) => {
            const point = (parameter.data as { meta?: Record<string, unknown> } | undefined)?.meta;
            const value = Array.isArray(parameter.value) ? parameter.value[1] : null;
            if (value === null || value === undefined) {
              const gap = (parameter.data as { gap?: { reason: string } } | undefined)?.gap;
              return `<div><span class="chart-tooltip-dot" style="background:${escapeHtml(parameter.color)}"></span>${escapeHtml(parameter.seriesName)}: ${escapeHtml(gap?.reason ?? "немає виміру")}</div>`;
            }
            const min = typeof point?.min_bps === "number" ? formatMbps(point.min_bps) : "—";
            const max = typeof point?.max_bps === "number" ? formatMbps(point.max_bps) : "—";
            const quality = point?.quality === "PARTIAL" ? " · неповні дані" : "";
            return `<div><span class="chart-tooltip-dot" style="background:${escapeHtml(parameter.color)}"></span>${escapeHtml(parameter.seriesName)}: <b>${escapeHtml(formatMbps(Number(value) * 1_000_000))}</b><br><span class="chart-tooltip-muted">min ${escapeHtml(min)} / max ${escapeHtml(max)}${escapeHtml(quality)}</span></div>`;
          }).join("");
          return `<div class="chart-tooltip-muted">${escapeHtml(formatDateTime(typeof at === "number" ? new Date(at).toISOString() : String(at ?? "")))}</div>${details}`;
        },
      },
      xAxis: [
        {
          type: "time", gridIndex: 0, min: from, max: to,
          axisLine: { lineStyle: { color: "#33444f" } },
          axisTick: { show: false },
          axisLabel: { color: "#8fa3af", hideOverlap: true, formatter: (value: number) => formatTime(new Date(value).toISOString(), showDateTicks ? { day: "2-digit", month: "2-digit" } : { hour: "2-digit", minute: "2-digit" }) },
          splitLine: { show: false },
        },
        {
          type: "time", gridIndex: 1, min: from, max: to,
          axisLine: { lineStyle: { color: "#33444f" } },
          axisTick: { show: false },
          axisLabel: { show: false },
          splitLine: { show: false },
        },
      ],
      yAxis: [
        {
          type: "value", gridIndex: 0, name: "Mbps", min: 0,
          nameTextStyle: { color: "#8fa3af", padding: [0, 0, 0, -10] },
          axisLabel: { color: "#8fa3af", formatter: (value: number) => value === 0 ? "0" : `${value}` },
          axisLine: { show: false }, axisTick: { show: false },
          splitLine: { lineStyle: { color: "#25343e", type: "dashed" } },
        },
        {
          type: "category", gridIndex: 1, data: lanes, inverse: true,
          axisLabel: { color: "#b3c1c9", fontSize: 11, width: 116, overflow: "truncate" },
          axisLine: { lineStyle: { color: "#33444f" } }, axisTick: { show: false },
          splitLine: { show: true, lineStyle: { color: "#202d36" } },
        },
      ],
      dataZoom: [
        { type: "inside", xAxisIndex: [0, 1], filterMode: "none", zoomOnMouseWheel: true, moveOnMouseMove: true },
        { type: "slider", xAxisIndex: [0, 1], bottom: 2, height: 18, borderColor: "#2b3a44", backgroundColor: "#152029", fillerColor: "#3f9a9a33", handleStyle: { color: "#65c9be" }, textStyle: { color: "#7e929f" } },
      ],
      series: [
        ...rangeSeries,
        ...lineSeries,
        {
          id: "event-timeline",
          name: "Події",
          type: "custom",
          xAxisIndex: 1,
          yAxisIndex: 1,
          clip: true,
          z: 5,
          data: eventData,
          renderItem: (params: CustomSeriesRenderItemParams, api: CustomSeriesRenderItemAPI) => {
            const start = Number(api.value(0));
            const end = Number(api.value(1));
            const lane = Number(api.value(2));
            const startPoint = api.coord([start, lane]);
            const endPoint = api.coord([end, lane]);
            const item = (params as CustomSeriesRenderItemParams & { data?: { itemStyle?: { color?: string } } }).data;
            const fill = item?.itemStyle?.color ?? "#78a7d4";
            if (end - start < 500) {
              return { type: "circle", shape: { cx: startPoint[0], cy: startPoint[1], r: 5 }, style: { fill, stroke: "#0c141a", lineWidth: 1.5 } };
            }
            return {
              type: "rect",
              shape: { x: startPoint[0], y: startPoint[1] - 5, width: Math.max(7, endPoint[0] - startPoint[0]), height: 10, r: 4 },
              style: { fill, opacity: 0.9, stroke: `${fill}cc`, lineWidth: 1 },
            };
          },
        },
      ] as EChartsOption["series"],
    };

    chart.setOption(option, { replaceMerge: ["series"], lazyUpdate: true });
  }, [seriesRows, eventRows, from, to, visible]);

  return (
    <div className="chart-frame">
      <div className="chart-legend" aria-label="Пункти спостереження на графіку">
        {seriesRows.map((row, index) => (
          <button
            className={`legend-item ${visible.has(row.probe.id) ? "is-visible" : "is-hidden"}`}
            key={row.probe.id}
            onClick={() => onToggleProbe(row.probe.id)}
            aria-pressed={visible.has(row.probe.id)}
            title={`${row.probe.name} · ${row.probe.role}`}
          >
            <span className="legend-swatch" style={{ backgroundColor: probeColor(index) }} />
            <span>{row.probe.name}</span>
            <small>{row.probe.role === "CLIENT" ? "клієнт" : row.probe.role === "SERVER_EGRESS" ? "сервер" : row.probe.role.toLowerCase().replaceAll("_", " ")}</small>
          </button>
        ))}
        <span className="legend-events"><i /> Події та інциденти</span>
      </div>
      <div className="chart-main" ref={hostRef} role="img" aria-label="Графік measured bitrate та подій по пунктах спостереження" />
      {!seriesRows.length && <div className="chart-empty">Ще немає пунктів спостереження для цього потоку.</div>}
    </div>
  );
}
