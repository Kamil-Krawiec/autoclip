import { useEffect, useMemo, useState } from 'react'

import {
  formatTimecode,
  type CropPath,
  type FramingSlice,
  type ManualFraming,
  type SingleFraming,
  type SlicesFraming,
} from '../api'
import { FramingEditor } from './FramingEditor'

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
  if (framing.mode === 'manual') return `Manual (${framing.zoom.toFixed(1)}×)`
  return 'Auto'
}

export function SliceFramingEditor({
  src,
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
  onResetToGlobalAuto,
}: {
  src: string
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
  onResetToGlobalAuto: () => Promise<boolean>
}) {
  const segments = useMemo(
    () => ensureClipCoverage(slicesFraming.segments, clipStartS, clipEndS),
    [slicesFraming.segments, clipStartS, clipEndS],
  )
  const [selectedIndex, setSelectedIndex] = useState(0)

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

  const visibleSlices = useMemo(() => {
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

  const handleSelectSlice = (index: number) => {
    if (index === activeIndex) return
    if (disabled || framingDirty) return
    setSelectedIndex(index)
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

  const handleChangeSelectedMode = async (nextMode: 'auto' | 'fit' | 'manual') => {
    if (disabled || framingDirty || !activeSlice) return
    if (activeSlice.framing.mode === nextMode) return
    let nextFraming: SingleFraming
    if (nextMode === 'auto') {
      nextFraming = { mode: 'auto' }
    } else if (nextMode === 'fit') {
      nextFraming = { mode: 'fit' }
    } else {
      nextFraming =
        activeSlice.framing.mode === 'manual' ? activeSlice.framing : { ...DEFAULT_MANUAL }
    }
    const nextSegments = segments.map((s, idx) =>
      idx === activeIndex ? { ...s, framing: nextFraming } : s,
    )
    await onCommitSlices({ mode: 'slices', segments: nextSegments })
  }

  const handleCommitSelectedManual = async (nextManual: ManualFraming): Promise<boolean> => {
    if (!activeSlice) return false
    const nextSegments = segments.map((s, idx) =>
      idx === activeIndex ? { ...s, framing: nextManual } : s,
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

  return (
    <div
      className="space-y-4 border border-ink-800 bg-ink-900/40 p-3"
      aria-label="Per-slice framing editor"
    >
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <div>
          <span className="eyebrow">Per-slice framing</span>
          <p className="mt-0.5 text-xs text-ink-400">
            Playhead at {formatTimecode(playheadRelS)} · Select a slice or split at the current playhead.
          </p>
        </div>
        <button
          type="button"
          disabled={disabled || framingDirty}
          onClick={() => void onResetToGlobalAuto()}
          className="btn btn-ghost text-xs"
        >
          Reset to global Auto
        </button>
      </div>

      <ul className="space-y-1.5" aria-label="Framing slices">
        {visibleSlices.map((entry, visIdx) => {
          const isSelected = entry.index === activeIndex
          return (
            <li key={`${entry.slice.start_s}-${entry.slice.end_s}-${entry.index}`}>
              <button
                type="button"
                aria-pressed={isSelected}
                disabled={disabled || framingDirty}
                onClick={() => handleSelectSlice(entry.index)}
                className={[
                  'flex w-full items-center justify-between border px-3 py-2 text-left text-xs transition-colors',
                  isSelected
                    ? 'border-sodium-500 bg-ink-850 text-ink-100'
                    : 'border-ink-800 bg-ink-900/60 text-ink-300 hover:border-ink-700',
                ].join(' ')}
              >
                <span className="numeric font-medium">
                  Slice {visIdx + 1}: {formatTimecode(entry.relStartS)} – {formatTimecode(entry.relEndS)}
                </span>
                <span className="text-sodium-500">{describeFraming(entry.slice.framing)}</span>
              </button>
            </li>
          )
        })}
      </ul>

      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          disabled={disabled || framingDirty || splitCandidateS === null}
          onClick={() => void handleSplitAtPlayhead()}
          className="btn btn-ghost text-xs"
        >
          Split slice at playhead ({formatTimecode(playheadRelS)})
        </button>
        <button
          type="button"
          disabled={disabled || framingDirty || !canMergeWithNext}
          onClick={() => void handleMergeWithNext()}
          className="btn btn-ghost text-xs"
        >
          Merge with next slice
        </button>
      </div>

      {activeSlice && activeEntry && (
        <div className="space-y-3 border-t border-ink-800 pt-3">
          <p className="text-xs text-ink-300">
            Selected slice ({formatTimecode(activeEntry.relStartS)} –{' '}
            {formatTimecode(activeEntry.relEndS)}) mode:
          </p>
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              disabled={disabled || framingDirty}
              onClick={() => void handleChangeSelectedMode('auto')}
              className={[
                'btn',
                activeSlice.framing.mode === 'auto' ? 'btn-primary' : 'btn-ghost',
              ].join(' ')}
            >
              Slice Auto
            </button>
            <button
              type="button"
              disabled={disabled || framingDirty}
              onClick={() => void handleChangeSelectedMode('fit')}
              className={[
                'btn',
                activeSlice.framing.mode === 'fit' ? 'btn-primary' : 'btn-ghost',
              ].join(' ')}
            >
              Slice Fit + blur
            </button>
            <button
              type="button"
              disabled={disabled || framingDirty || !hasVideoSource}
              onClick={() => void handleChangeSelectedMode('manual')}
              className={[
                'btn',
                activeSlice.framing.mode === 'manual' ? 'btn-primary' : 'btn-ghost',
              ].join(' ')}
            >
              Slice Manual
            </button>
          </div>

          {activeSlice.framing.mode === 'manual' && (
            <FramingEditor
              key={`${clipId}:slice:${activeIndex}:${activeEntry.visStartS}`}
              src={src}
              startS={activeEntry.visStartS}
              framing={activeSlice.framing}
              cropPath={selectedManualSubCropPath}
              disabled={disabled}
              onDirtyChange={onDirtyChange}
              onCommit={handleCommitSelectedManual}
            />
          )}
        </div>
      )}
    </div>
  )
}
