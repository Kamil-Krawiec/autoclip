import { useEffect, useMemo, useRef, useState, type MouseEvent } from 'react'

import {
  formatTimecode,
  api,
  type CropPath,
  type FramingSlice,
  type ManualFraming,
  type SplitFraming,
  type SingleFraming,
  type SlicesFraming,
  type Word,
} from '../api'
import { SplitFramingEditor, type SplitPanels } from './SplitFramingEditor'
import { FramingEditor } from './FramingEditor'
import { TrimBar } from './TrimBar'
import type { FramingGeometry, PanRequest } from './ClipPlayer'

const MIN_SLICE_DURATION_S = 0.1
const MAX_SLICES = 64
const EPSILON = 1e-6

const DEFAULT_MANUAL: ManualFraming = {
  mode: 'manual',
  center_x: 0.5,
  center_y: 0.5,
  zoom: 1,
}

const ASPECTS: Record<string, [number, number]> = {
  '9:16': [9, 16],
  '1:1': [1, 1],
  '16:9': [16, 9],
}

function computeManualSegmentSize(
  sourceW: number,
  sourceH: number,
  ratio: string,
  zoom: number,
): { width: number; height: number } {
  const [rw, rh] = ASPECTS[ratio] ?? ASPECTS['9:16']
  const targetAspect = rw / rh
  const sourceAspect = sourceW / sourceH
  let baseW = sourceW
  let baseH = sourceH
  if (sourceAspect > targetAspect) {
    baseW = sourceH * targetAspect
  } else {
    baseH = sourceW / targetAspect
  }
  const safeZoom = Math.max(1, Math.min(4, Number.isFinite(zoom) ? zoom : 1))
  return {
    width: Math.max(2, Math.round(baseW / safeZoom)),
    height: Math.max(2, Math.round(baseH / safeZoom)),
  }
}

/**
 * Ensure the current clip range [clipStartS, clipEndS] is covered by authored
 * slices so the user can split and edit anywhere inside the current trim while
 * preserving any hidden authored slices outside the current trim.
 */
function ensureClipCoverage(
  segments: FramingSlice[],
  clipStartS: number,
  clipEndS: number,
): FramingSlice[] {
  if (segments.length === 0) {
    return [{ start_s: clipStartS, end_s: clipEndS, framing: { mode: 'auto' } }]
  }
  const next = segments.map((s) => ({ ...s, framing: { ...s.framing } }))
  const first = next[0]
  const last = next[next.length - 1]
  if (clipStartS < first.start_s - EPSILON) {
    next.unshift({
      start_s: clipStartS,
      end_s: first.start_s,
      framing: { mode: 'auto' },
    })
  }
  if (clipEndS > last.end_s + EPSILON) {
    next.push({
      start_s: last.end_s,
      end_s: clipEndS,
      framing: { mode: 'auto' },
    })
  }
  return next
}

function describeFraming(framing: SingleFraming): string {
  if (framing.mode === 'fit') return 'Fit'
  if (framing.mode === 'split') return 'Split'
  if (framing.mode === 'manual') return `Manual (${framing.zoom.toFixed(1)}×)`
  return 'Auto'
}

type VisibleSliceEntry = {
  index: number
  slice: FramingSlice
  relStartS: number
  relEndS: number
  visStartS: number
  visEndS: number
}

function clamp01(value: number): number {
  if (!Number.isFinite(value)) return 0
  return Math.min(1, Math.max(0, value))
}

export function SliceFramingEditor({
  clipId,
  clipStartS,
  clipEndS,
  ratio,
  slicesFraming,
  cropPath,
  sourceWidth,
  sourceHeight,
  sourceDuration,
  hasVideoSource,
  playheadSourceS,
  removedRanges,
  onCommitRemoved,
  disabled,
  framingDirty,
  onDirtyChange,
  onCommitSlices,
  onSeek,
  words,
  onTrim,
  onDraftGeometry,
  onDraftSplit,
  panRequest,
}: {
  clipId: string
  clipStartS: number
  clipEndS: number
  ratio: string
  slicesFraming: SlicesFraming
  cropPath: CropPath | null
  sourceWidth: number
  sourceHeight: number
  sourceDuration?: number
  hasVideoSource: boolean
  playheadSourceS: number
  removedRanges: { start_s: number; end_s: number }[]
  onCommitRemoved: (ranges: { start_s: number; end_s: number }[]) => Promise<boolean>
  disabled: boolean
  framingDirty: boolean
  onDirtyChange: (dirty: boolean) => void
  onCommitSlices: (next: SlicesFraming) => Promise<boolean>
  /** Seek the clip player to a source-time position (seconds), e.g. when the user clicks the timeline. */
  onSeek: (sourceTimeS: number) => void
  words: Word[]
  onTrim: (start: number, end: number) => void
  onDraftGeometry: (geometry: FramingGeometry | null) => void
  onDraftSplit: (panels: SplitPanels | null) => void
  panRequest: PanRequest | null
}) {
  const segments = useMemo(
    () => ensureClipCoverage(slicesFraming.segments, clipStartS, clipEndS),
    [slicesFraming.segments, clipStartS, clipEndS],
  )
  const [selectedIndex, setSelectedIndex] = useState(0)
  const [zoom, setZoom] = useState(1)
  const [visuals, setVisuals] = useState<{ strip: string; peaks: number[] } | null>(null)
  const [visualError, setVisualError] = useState(false)
  const [visualRetry, setVisualRetry] = useState(0)

  useEffect(() => {
    const abort = new AbortController()
    setVisuals(null)
    setVisualError(false)
    api.getTimeline(clipId, abort.signal).then(setVisuals).catch(() => {
      if (!abort.signal.aborted) setVisualError(true)
    })
    return () => abort.abort()
  }, [clipId, clipStartS, clipEndS, visualRetry])
  const timelinePointerStart = useRef<{ id: number; x: number; y: number } | null>(null)
  const suppressTimelineClick = useRef(false)

  // Keep selected index valid and prefer visible slices when clip or segments change.
  useEffect(() => {
    setSelectedIndex((current) => {
      if (current >= 0 && current < segments.length) {
        const seg = segments[current]
        if (seg.end_s > clipStartS + EPSILON && seg.start_s < clipEndS - EPSILON) {
          return current
        }
      }
      const firstVisible = segments.findIndex(
        (s) => s.end_s > clipStartS + EPSILON && s.start_s < clipEndS - EPSILON,
      )
      return firstVisible >= 0 ? firstVisible : 0
    })
  }, [clipId, segments, clipStartS, clipEndS])

  const visibleSlices = useMemo((): VisibleSliceEntry[] => {
    return segments
      .map((slice, index) => {
        const visStart = Math.max(clipStartS, slice.start_s)
        const visEnd = Math.min(clipEndS, slice.end_s)
        if (visEnd - visStart <= EPSILON) return null
        return {
          index,
          slice,
          relStartS: Math.max(0, visStart - clipStartS),
          relEndS: Math.max(0, visEnd - clipStartS),
          visStartS: visStart,
          visEndS: visEnd,
        }
      })
      .filter((item): item is NonNullable<typeof item> => item !== null)
  }, [segments, clipStartS, clipEndS])

  useEffect(() => {
    if (disabled || framingDirty || timelinePointerStart.current) return
    const current = visibleSlices.find(
      (entry) => playheadSourceS >= entry.visStartS && playheadSourceS < entry.visEndS,
    )
    if (current) setSelectedIndex(current.index)
  }, [visibleSlices, playheadSourceS, disabled, framingDirty])

  const activeEntry =
    visibleSlices.find((v) => v.index === selectedIndex) ?? visibleSlices[0] ?? null
  const activeIndex = activeEntry?.index ?? 0
  const activeSlice = segments[activeIndex] ?? null

  const splitCandidateS = useMemo(() => {
    if (!activeSlice) return null
    const minSplit = Math.max(clipStartS, activeSlice.start_s) + MIN_SLICE_DURATION_S
    const maxSplit = Math.min(clipEndS, activeSlice.end_s) - MIN_SLICE_DURATION_S
    if (maxSplit < minSplit - EPSILON) return null
    if (
      !Number.isFinite(playheadSourceS) ||
      playheadSourceS < minSplit - EPSILON ||
      playheadSourceS > maxSplit + EPSILON ||
      segments.length >= MAX_SLICES
    ) {
      return null
    }
    const clamped = Math.min(maxSplit, Math.max(minSplit, playheadSourceS))
    if (
      clamped - activeSlice.start_s < MIN_SLICE_DURATION_S - EPSILON ||
      activeSlice.end_s - clamped < MIN_SLICE_DURATION_S - EPSILON
    ) {
      return null
    }
    return Number(clamped.toFixed(4))
  }, [activeSlice, clipStartS, clipEndS, playheadSourceS, segments.length])

  const canMergeWithNext = useMemo(() => {
    if (!activeEntry) return false
    const nextVisible = visibleSlices.find((v) => v.index === activeEntry.index + 1)
    return Boolean(nextVisible)
  }, [activeEntry, visibleSlices])

  const handleTimelineSliceClick = (
    entry: VisibleSliceEntry,
    event: MouseEvent<HTMLButtonElement>,
  ) => {
    if (disabled || framingDirty) return
    // Selection still applies when re-clicking the selected slice (it re-seeks).
    setSelectedIndex(entry.index)
    const visDuration = entry.visEndS - entry.visStartS
    let clickedSourceS: number
    if (event.nativeEvent.detail === 0) {
      // Keyboard activation (Enter/Space) reports no pointer position.
      clickedSourceS = entry.visStartS
    } else {
      const rect = event.currentTarget.getBoundingClientRect()
      const fraction = rect.width > 0 ? (event.clientX - rect.left) / rect.width : 0
      clickedSourceS = entry.visStartS + clamp01(fraction) * visDuration
    }
    onSeek(Number(clickedSourceS.toFixed(4)))
  }

  const handleSplitAtPlayhead = async () => {
    if (disabled || framingDirty || splitCandidateS === null || !activeSlice) return
    if (segments.length >= MAX_SLICES) return
    if (
      splitCandidateS - activeSlice.start_s < MIN_SLICE_DURATION_S - EPSILON ||
      activeSlice.end_s - splitCandidateS < MIN_SLICE_DURATION_S - EPSILON
    ) {
      return
    }
    const firstHalf: FramingSlice = {
      start_s: activeSlice.start_s,
      end_s: splitCandidateS,
      framing: { ...activeSlice.framing },
    }
    const secondHalf: FramingSlice = {
      start_s: splitCandidateS,
      end_s: activeSlice.end_s,
      framing: { ...activeSlice.framing },
    }
    const nextSegments = [
      ...segments.slice(0, activeIndex),
      firstHalf,
      secondHalf,
      ...segments.slice(activeIndex + 1),
    ]
    const ok = await onCommitSlices({ mode: 'slices', segments: nextSegments })
    if (ok) {
      setSelectedIndex(activeIndex + 1)
    }
  }

  const handleMergeWithNext = async () => {
    if (disabled || framingDirty || !canMergeWithNext || !activeSlice) return
    const nextSlice = segments[activeIndex + 1]
    if (!nextSlice) return
    const merged: FramingSlice = {
      start_s: activeSlice.start_s,
      end_s: nextSlice.end_s,
      framing: { ...activeSlice.framing },
    }
    const nextSegments = [
      ...segments.slice(0, activeIndex),
      merged,
      ...segments.slice(activeIndex + 2),
    ]
    await onCommitSlices({ mode: 'slices', segments: nextSegments })
  }

  const selectedIsRemoved = activeEntry && removedRanges.some(range => range.start_s <= activeEntry.visStartS + EPSILON && range.end_s >= activeEntry.visEndS - EPSILON)

  const toggleRemoved = async () => {
    if (!activeEntry) return
    const { visStartS: start, visEndS: end } = activeEntry
    const next = selectedIsRemoved
      ? removedRanges.flatMap(range => range.end_s <= start || range.start_s >= end ? [range] : [
          ...(range.start_s < start ? [{ start_s: range.start_s, end_s: start }] : []),
          ...(range.end_s > end ? [{ start_s: end, end_s: range.end_s }] : []),
        ])
      : [...removedRanges, { start_s: start, end_s: end }].sort((a, b) => a.start_s - b.start_s).reduce<{ start_s: number; end_s: number }[]>((result, range) => {
          const last = result[result.length - 1]
          if (last && range.start_s <= last.end_s) last.end_s = Math.max(last.end_s, range.end_s)
          else result.push({ ...range })
          return result
        }, [])
    await onCommitRemoved(next)
  }

  const moveBoundaryToPlayhead = async () => {
    const next = segments[activeIndex + 1]
    if (!activeSlice || !next || playheadSourceS <= activeSlice.start_s + MIN_SLICE_DURATION_S || playheadSourceS >= next.end_s - MIN_SLICE_DURATION_S) return
    await onCommitSlices({ mode: 'slices', segments: segments.map((segment, index) => index === activeIndex
      ? { ...segment, end_s: playheadSourceS }
      : index === activeIndex + 1 ? { ...segment, start_s: playheadSourceS } : segment) })
  }

  const movePreviousBoundaryToPlayhead = async () => {
    const previous = segments[activeIndex - 1]
    if (!activeSlice || !previous || playheadSourceS <= previous.start_s + MIN_SLICE_DURATION_S || playheadSourceS >= activeSlice.end_s - MIN_SLICE_DURATION_S) return
    await onCommitSlices({ mode: 'slices', segments: segments.map((segment, index) => index === activeIndex - 1
      ? { ...segment, end_s: playheadSourceS }
      : index === activeIndex ? { ...segment, start_s: playheadSourceS } : segment) })
  }

  const handleChangeSelectedMode = async (nextMode: 'auto' | 'fit' | 'manual' | 'split') => {
    if (disabled || framingDirty || !activeSlice) return
    if (activeSlice.framing.mode === nextMode) return
    let nextFraming: SingleFraming
    if (nextMode === 'auto') {
      nextFraming = { mode: 'auto' }
    } else if (nextMode === 'fit') {
      nextFraming = { mode: 'fit' }
    } else if (nextMode === 'split') {
      nextFraming = { mode: 'split' }
    } else {
      nextFraming =
        activeSlice.framing.mode === 'manual' ? activeSlice.framing : { ...DEFAULT_MANUAL }
    }
    const nextSegments = segments.map((s, idx) =>
      idx === activeIndex ? { ...s, framing: nextFraming } : s,
    )
    await onCommitSlices({ mode: 'slices', segments: nextSegments })
  }

  const handleCommitSelectedFraming = async (nextFraming: ManualFraming | SplitFraming): Promise<boolean> => {
    if (!activeSlice) return false
    const nextSegments = segments.map((s, idx) =>
      idx === activeIndex ? { ...s, framing: nextFraming } : s,
    )
    return onCommitSlices({ mode: 'slices', segments: nextSegments })
  }

  // Build a sub-CropPath whose segments[0] corresponds to the selected manual slice
  // so FramingEditor computes exact bounds for this slice rather than segments[0] of the clip.
  const selectedManualSubCropPath = useMemo((): CropPath | null => {
    if (!activeSlice || activeSlice.framing.mode !== 'manual') return null
    const srcW = cropPath?.source_width || sourceWidth
    const srcH = cropPath?.source_height || sourceHeight
    if (!srcW || !srcH) return null

    const relStart = Math.max(0, activeSlice.start_s - clipStartS)
    const relEnd = Math.max(relStart + 0.01, activeSlice.end_s - clipStartS)
    const midElapsed = (relStart + relEnd) / 2

    const matchingSeg =
      cropPath?.segments.find(
        (seg) => !seg.fit && midElapsed >= seg.start_s - 1e-3 && midElapsed <= seg.end_s + 1e-3,
      ) ?? null

    if (matchingSeg && matchingSeg.width > 0 && matchingSeg.height > 0) {
      return {
        source_width: srcW,
        source_height: srcH,
        segments: [matchingSeg],
      }
    }

    const computed = computeManualSegmentSize(srcW, srcH, ratio, activeSlice.framing.zoom)
    return {
      source_width: srcW,
      source_height: srcH,
      segments: [
        {
          start_s: relStart,
          end_s: relEnd,
          width: computed.width,
          height: computed.height,
          keyframes: [{ t: relStart, x: 0, y: 0 }],
          strategy: 'general',
          zoom: activeSlice.framing.zoom,
          fit: false,
        },
      ],
    }
  }, [activeSlice, cropPath, sourceWidth, sourceHeight, clipStartS, ratio])

  const waveformMax = Math.max(0.01, ...(visuals?.peaks ?? []))
  const playheadRelS = Math.max(0, playheadSourceS - clipStartS)
  const clipDurationS = Math.max(EPSILON, clipEndS - clipStartS)
  const playheadPct = clamp01(playheadRelS / clipDurationS) * 100
  const playheadVisible =
    playheadRelS >= -EPSILON && playheadRelS <= clipDurationS + EPSILON

  const seekFromTimelinePointer = (event: React.PointerEvent<HTMLDivElement>) => {
    const timeline = event.currentTarget.closest('[aria-label="Framing slices"]')
    const rect = timeline?.getBoundingClientRect()
    if (!rect || rect.width <= 0) return
    const fraction = clamp01((event.clientX - rect.left) / rect.width)
    onSeek(Number((clipStartS + fraction * clipDurationS).toFixed(4)))
  }

  const handleTimelinePointerDown = (event: React.PointerEvent<HTMLDivElement>) => {
    if (disabled || framingDirty || event.button !== 0 || (event.target instanceof Element && event.target.closest('button'))) return
    timelinePointerStart.current = { id: event.pointerId, x: event.clientX, y: event.clientY }
    suppressTimelineClick.current = false
    event.currentTarget.setPointerCapture(event.pointerId)
    seekFromTimelinePointer(event)
  }

  const handleTimelinePointerMove = (event: React.PointerEvent<HTMLDivElement>) => {
    if (!event.currentTarget.hasPointerCapture(event.pointerId)) return
    const start = timelinePointerStart.current
    if (start?.id === event.pointerId && Math.hypot(event.clientX - start.x, event.clientY - start.y) > 4) {
      suppressTimelineClick.current = true
    }
    seekFromTimelinePointer(event)
  }

  const handleTimelineClickCapture = (event: React.MouseEvent<HTMLDivElement>) => {
    if (!suppressTimelineClick.current) return
    suppressTimelineClick.current = false
    event.preventDefault()
    event.stopPropagation()
  }

  const handlePlayheadPointerDown = (event: React.PointerEvent<HTMLDivElement>) => {
    if (disabled || framingDirty || event.button !== 0) return
    event.preventDefault()
    event.stopPropagation()
    event.currentTarget.setPointerCapture(event.pointerId)
    timelinePointerStart.current = { id: event.pointerId, x: event.clientX, y: event.clientY }
    seekFromTimelinePointer(event)
  }

  return (
    <div
      className="space-y-4 border border-ink-800 bg-ink-900/40 p-3"
      aria-label="Per-slice framing editor"
      onClickCapture={(event) => {
        if (event.detail > 0 && event.target instanceof HTMLElement) event.target.closest('button')?.blur()
      }}
    >
      <div className="flex items-center justify-between text-xs text-ink-300">
        <span>{visualError ? <button type="button" onClick={() => setVisualRetry(value => value + 1)}>Timeline media unavailable · Retry</button> : visuals ? 'Scroll to explore the timeline' : 'Loading timeline media…'}</span>
        <label>Zoom <input aria-label="Timeline zoom" type="range" min="1" max="8" step="1" value={zoom} onChange={event => setZoom(Number(event.target.value))} /></label>
      </div>
      <TrimBar words={words} startS={clipStartS} endS={clipEndS}
        originalStart={clipStartS} originalEnd={clipEndS} sourceDuration={sourceDuration} onCommit={onTrim}
        disabled={disabled || framingDirty}>
      <div className="h-full w-full overflow-x-auto">
      <div
        role="group"
        aria-label="Framing slices"
        onPointerDown={handleTimelinePointerDown}
        onPointerMove={handleTimelinePointerMove}
        onClickCapture={handleTimelineClickCapture}
        onPointerUp={(event) => {
          if (event.currentTarget.hasPointerCapture(event.pointerId)) {
            event.currentTarget.releasePointerCapture(event.pointerId)
          }
          timelinePointerStart.current = null
        }}
        onPointerCancel={(event) => {
          if (event.currentTarget.hasPointerCapture(event.pointerId)) {
            event.currentTarget.releasePointerCapture(event.pointerId)
          }
          timelinePointerStart.current = null
          suppressTimelineClick.current = false
        }}
        style={{ touchAction: 'none', width: `${zoom * 100}%` }}
        className="relative h-full"
      >
        <div aria-label="Timeline time ruler" className="pointer-events-none absolute inset-x-0 top-0 z-[3] flex justify-between bg-black/60 px-1 text-[10px] text-white">
          <span>0:00</span><span>{formatTimecode(clipDurationS / 2)}</span><span>{formatTimecode(clipDurationS)}</span>
        </div>
        {visuals?.strip && <img aria-label="Video thumbnails" className="absolute inset-x-0 top-0 h-1/2 w-full object-fill" src={visuals.strip} />}
        {visuals && visuals.peaks.length > 0 && <svg aria-label="Audio waveform" className="pointer-events-none absolute inset-x-0 bottom-0 z-[1] h-1/2 w-full bg-ink-900" viewBox={`0 0 ${visuals.peaks.length} 100`} preserveAspectRatio="none">{visuals.peaks.map((peak, index) => <line key={index} x1={index + 0.5} x2={index + 0.5} y1={50 - peak / waveformMax * 45} y2={50 + peak / waveformMax * 45} stroke="#f7b65a" strokeOpacity="0.9" strokeWidth="0.65" />)}</svg>}
        <div className="relative flex h-full w-full divide-x divide-ink-800 overflow-hidden border border-ink-800">
          {visibleSlices.map((entry, visIdx) => {
            const widthPct =
              clamp01((entry.visEndS - entry.visStartS) / clipDurationS) * 100
            const isSelected = entry.index === activeIndex
            const label = `Slice ${visIdx + 1}: ${formatTimecode(entry.relStartS)} – ${formatTimecode(entry.relEndS)} ${describeFraming(entry.slice.framing)}`
            return (
              <button
                key={`${entry.slice.start_s}-${entry.slice.end_s}-${entry.index}`}
                type="button"
                aria-pressed={isSelected}
                aria-label={label}
                title={label}
                aria-disabled={disabled || framingDirty}
                disabled={disabled || framingDirty}
                onClick={(event) => handleTimelineSliceClick(entry, event)}
                className={[
                  'flex min-w-0 items-center justify-center overflow-hidden text-center text-[10px] leading-tight transition-colors',
                  isSelected
                    ? 'bg-sodium-500/25 text-ink-100 ring-1 ring-inset ring-sodium-500'
                    : 'bg-ink-900/25 text-ink-100 hover:bg-ink-850/40',
                ].join(' ')}
                style={{ width: `${widthPct}%` }}
              >
                <span className={widthPct >= 8 ? 'truncate px-0.5' : 'sr-only'}>
                  {label}
                </span>
              </button>
            )
          })}
        </div>
        {removedRanges.map((range, index) => {
          const left = clamp01((range.start_s - clipStartS) / clipDurationS) * 100
          const right = clamp01((range.end_s - clipStartS) / clipDurationS) * 100
          return right > left ? <div key={index} aria-label="Removed section" className="pointer-events-none absolute inset-y-0 z-[2] bg-red-900/60" style={{ left: `${left}%`, width: `${right - left}%` }} /> : null
        })}
        {playheadVisible && (
          <div
            aria-hidden="true"
            data-testid="timeline-playhead"
            onPointerDown={handlePlayheadPointerDown}
            onPointerMove={handleTimelinePointerMove}
            onPointerUp={(event) => {
              if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId)
              timelinePointerStart.current = null
            }}
            onPointerCancel={(event) => {
              if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId)
              timelinePointerStart.current = null
              suppressTimelineClick.current = false
            }}
            className="absolute bottom-0 top-0 z-10 w-3 -translate-x-1/2 cursor-ew-resize touch-none"
            style={{ left: `calc(${playheadPct}% - 1px)` }}
          >
            <div className="pointer-events-none absolute inset-x-[5px] h-full bg-sodium-500" />
            <span className="pointer-events-none absolute -top-1 left-1/2 block h-2 w-2 -translate-x-1/2 rounded-full bg-sodium-500" />
          </div>
        )}
      </div>
      </div>
      </TrimBar>

      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          disabled={disabled || framingDirty || splitCandidateS === null}
          aria-label={`Split slice at playhead (${formatTimecode(playheadRelS)})`}
          onClick={() => void handleSplitAtPlayhead()}
          className="btn btn-ghost text-xs"
        >
          ✂ Split
        </button>
        <button type="button" className="btn btn-ghost text-xs" disabled={disabled || framingDirty || !activeEntry} onClick={() => void toggleRemoved()}>
          {selectedIsRemoved ? 'Restore section' : 'Remove section'}
        </button>
        <button type="button" className="btn btn-ghost text-xs" disabled={disabled || framingDirty || activeIndex === 0} onClick={() => void movePreviousBoundaryToPlayhead()}>
          Move previous boundary to playhead
        </button>
        <button type="button" className="btn btn-ghost text-xs" disabled={disabled || framingDirty || !canMergeWithNext} onClick={() => void moveBoundaryToPlayhead()}>
          Move next boundary to playhead
        </button>
        <button
          type="button"
          disabled={disabled || framingDirty || !canMergeWithNext}
          aria-label="Merge with next slice"
          onClick={() => void handleMergeWithNext()}
          className="btn btn-ghost text-xs"
        >
          Merge next
        </button>
      </div>

      {removedRanges.length > 0 && <div className="flex flex-wrap gap-2" aria-label="Removed sections">
        {removedRanges.map((range, index) => <button key={`${range.start_s}-${range.end_s}`} type="button" className="btn btn-ghost text-xs" disabled={disabled || framingDirty} onClick={() => void onCommitRemoved(removedRanges.filter((_, i) => i !== index))}>Restore {formatTimecode(range.start_s - clipStartS)} – {formatTimecode(range.end_s - clipStartS)}</button>)}
      </div>}
      {activeSlice && activeEntry && (
        <div className="space-y-3 border-t border-ink-800 pt-3">
          <p className="text-xs text-ink-300">
            Selected: {formatTimecode(activeEntry.relStartS)} – {formatTimecode(activeEntry.relEndS)}
          </p>
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              disabled={disabled || framingDirty}
              aria-label="Slice Auto"
              onClick={() => void handleChangeSelectedMode('auto')}
              className={[
                'btn',
                activeSlice.framing.mode === 'auto' ? 'btn-primary' : 'btn-ghost',
              ].join(' ')}
            >
              Auto
            </button>
            <button
              type="button"
              disabled={disabled || framingDirty}
              aria-label="Slice Fit + blur"
              onClick={() => void handleChangeSelectedMode('fit')}
              className={[
                'btn',
                activeSlice.framing.mode === 'fit' ? 'btn-primary' : 'btn-ghost',
              ].join(' ')}
            >
              Fit + blur
            </button>
            <button
              type="button"
              disabled={disabled || framingDirty || ratio !== '9:16' || sourceWidth <= sourceHeight || !hasVideoSource}
              title="Split a landscape source into two stacked halves for 9:16"
              onClick={() => void handleChangeSelectedMode('split')}
              className={['btn', activeSlice.framing.mode === 'split' ? 'btn-primary' : 'btn-ghost'].join(' ')}
            >
              Split screen
            </button>
            <button
              type="button"
              disabled={disabled || framingDirty || !hasVideoSource}
              aria-label="Slice Manual"
              onClick={() => void handleChangeSelectedMode('manual')}
              className={[
                'btn',
                activeSlice.framing.mode === 'manual' ? 'btn-primary' : 'btn-ghost',
              ].join(' ')}
            >
              Resize
            </button>
          </div>

            {activeSlice.framing.mode === 'split' && (
              <SplitFramingEditor
                key={`${clipId}:split:${activeIndex}:${activeEntry.visStartS}`}
                framing={activeSlice.framing}
                disabled={disabled}
                onDirtyChange={onDirtyChange}
                onCommit={handleCommitSelectedFraming}
                onDraftSplit={onDraftSplit}
                panRequest={panRequest}
              />
            )}
            {activeSlice.framing.mode === 'manual' && (
            <FramingEditor
              key={`${clipId}:slice:${activeIndex}:${activeEntry.visStartS}`}
              startS={activeEntry.visStartS}
              framing={activeSlice.framing}
              cropPath={selectedManualSubCropPath}
              disabled={disabled}
              onDirtyChange={onDirtyChange}
              onCommit={handleCommitSelectedFraming}
              onDraftGeometry={onDraftGeometry}
              panRequest={panRequest}
            />
          )}
        </div>
      )}
    </div>
  )
}
