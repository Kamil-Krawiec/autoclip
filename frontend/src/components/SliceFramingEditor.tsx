import { useEffect, useMemo, useRef, useState, type MouseEvent } from 'react'

import {
  formatTimecode,
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
  hasVideoSource,
  playheadSourceS,
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
  hasVideoSource: boolean
  playheadSourceS: number
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
    >
      <TrimBar words={words} startS={clipStartS} endS={clipEndS}
        originalStart={clipStartS} originalEnd={clipEndS} onCommit={onTrim}
        disabled={disabled || framingDirty}>
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
        style={{ touchAction: 'none' }}
        className="relative h-full"
      >
        <div className="flex h-full w-full divide-x divide-ink-800 overflow-hidden border border-ink-800 bg-ink-900/60">
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
                    : 'bg-ink-900/60 text-ink-300 hover:bg-ink-850/60',
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
