import { useCallback, useEffect, useRef, useState } from 'react'

import {
  formatTimecode,
  type CaptionOverrides,
  type CaptionStyle,
  type CropPath,
  type CropSegment,
  type Word,
} from '../api'

const VOLUME_KEY = 'autoclip.volume'

/** Output shapes, matching autoclip.pipeline.export.RATIOS. */
const ASPECTS: Record<string, [number, number]> = {
  '9:16': [9, 16],
  '1:1': [1, 1],
  '16:9': [16, 9],
}

/** Tallest the preview may be, so the transport row stays on screen. */
const MAX_HEIGHT_VH = 45

export type FramingGeometry = { widthNorm: number; heightNorm: number; leftNorm: number; topNorm: number }
export type PanRequest = { dx: number; dy: number; id: number }

function readStoredVolume(): number {
  const stored = Number(window.localStorage.getItem(VOLUME_KEY))
  return Number.isFinite(stored) && stored > 0 && stored <= 1 ? stored : 1
}

/**
 * 9:16 preview of one clip, with a CSS approximation of the burned captions.
 *
 * The approximation is deliberate: rendering the real ASS would mean shipping a
 * subtitle engine to the browser. What matters at review time is timing, word
 * grouping, and whether the style reads at all — the final look comes from
 * libass at export.
 */
export function ClipPlayer({
  src,
  startS,
  endS,
  words,
  style,
  ratio,
  cropPath,
  burnCaptions = true,
  captionOverrides,
  onPlayheadChange,
  seekRequest,
  draftGeometry,
  onPan,
}: {
  src: string
  startS: number
  endS: number
  words: Word[]
  style: CaptionStyle | undefined
  ratio: string
  cropPath?: CropPath | null
  burnCaptions?: boolean
  captionOverrides?: CaptionOverrides | null
  onPlayheadChange?: (sourceTimeS: number) => void
  seekRequest?: { time: number; id: number } | null
  draftGeometry?: FramingGeometry | null
  onPan?: (dx: number, dy: number) => void
}) {
  const video = useRef<HTMLVideoElement>(null)
  const bgVideo = useRef<HTMLVideoElement>(null)
  const drag = useRef<{ x: number; y: number; moved: boolean } | null>(null)
  const suppressClick = useRef(false)
  const [playing, setPlaying] = useState(false)
  const [time, setTime] = useState(startS)
  const [volume, setVolume] = useState(readStoredVolume)
  const [muted, setMuted] = useState(false)
  const [nativeControls, setNativeControls] = useState(false)
  const [audioCheck, setAudioCheck] = useState<AudioCheck | null>(null)
  const [checking, setChecking] = useState(false)
  const onPlayheadChangeRef = useRef(onPlayheadChange)
  onPlayheadChangeRef.current = onPlayheadChange

  const updateTime = useCallback((nextTime: number) => {
    setTime(nextTime)
    onPlayheadChangeRef.current?.(nextTime)
  }, [])

  const runAudioCheck = async () => {
    const element = video.current
    if (!element) return
    setChecking(true)
    setAudioCheck(null)
    try {
      setAudioCheck(await measureOutputLevel(element))
    } catch (error) {
      setAudioCheck({ ok: false, peakDb: null, detail: String(error) })
    } finally {
      setChecking(false)
      setPlaying(!element.paused)
    }
  }

  // Kept in sync imperatively: volume and muted are element properties, not
  // attributes, so React won't apply them from JSX on later renders.
  useEffect(() => {
    const element = video.current
    if (!element) return
    element.volume = volume
    element.muted = muted
    window.localStorage.setItem(VOLUME_KEY, String(volume))
  }, [volume, muted])

  const syncBackground = useCallback((forceTime = false) => {
    const fg = video.current
    const bg = bgVideo.current
    if (!fg || !bg) return
    bg.muted = true
    bg.volume = 0
    if (bg.playbackRate !== fg.playbackRate) {
      bg.playbackRate = fg.playbackRate
    }
    if (forceTime || Math.abs(bg.currentTime - fg.currentTime) > 0.08) {
      try {
        bg.currentTime = fg.currentTime
      } catch {
        /* Ignore seek errors before metadata is ready. */
      }
    }
    if (fg.paused) {
      if (!bg.paused) bg.pause()
    } else if (bg.paused) {
      void bg.play().catch(() => {})
    }
  }, [])

  useEffect(() => {
    const element = video.current
    if (!element || !seekRequest) return
    element.currentTime = Math.max(startS, Math.min(endS, seekRequest.time))
    updateTime(element.currentTime)
    syncBackground(true)
  }, [seekRequest, startS, endS, updateTime, syncBackground])

  // Re-seek whenever the clip or its trim changes, so the preview always starts
  // where the export will.
  useEffect(() => {
    const element = video.current
    if (!element) return
    element.currentTime = startS
    if (bgVideo.current) {
      bgVideo.current.pause()
      bgVideo.current.currentTime = startS
    }
    updateTime(startS)
    setPlaying(false)
    element.pause()
  }, [src, startS, updateTime])

  useEffect(() => {
    const fg = video.current
    const bg = bgVideo.current
    return () => {
      fg?.pause()
      bg?.pause()
    }
  }, [])

  const onTimeUpdate = useCallback(() => {
    const element = video.current
    if (!element) return
    if (element.currentTime >= endS) {
      element.pause()
      element.currentTime = startS
      if (bgVideo.current) {
        bgVideo.current.pause()
        bgVideo.current.currentTime = startS
      }
      setPlaying(false)
      updateTime(startS)
      return
    }
    updateTime(element.currentTime)
    syncBackground(false)
  }, [endS, startS, syncBackground, updateTime])

  const toggle = () => {
    const element = video.current
    if (!element) return
    if (element.paused) {
      if (element.currentTime < startS || element.currentTime >= endS) {
        element.currentTime = startS
        updateTime(startS)
        if (bgVideo.current) {
          bgVideo.current.currentTime = startS
        }
      }
      void element.play()
      if (bgVideo.current) {
        syncBackground(true)
        void bgVideo.current.play().catch(() => {})
      }
      setPlaying(true)
    } else {
      element.pause()
      bgVideo.current?.pause()
      setPlaying(false)
    }
  }

  const elapsed = Math.max(0, time - startS)
  const duration = Math.max(0.01, endS - startS)

  const activeSegment = activeCropSegment(cropPath, elapsed)
  const isFit = !draftGeometry && Boolean(activeSegment?.fit)
  const isSplit = !draftGeometry && Boolean(activeSegment?.split)
  const cropStyle = draftGeometry ? {
    width: `${100 / draftGeometry.widthNorm}%`,
    height: `${100 / draftGeometry.heightNorm}%`,
    left: `${-100 * draftGeometry.leftNorm / draftGeometry.widthNorm}%`,
    top: `${-100 * draftGeometry.topNorm / draftGeometry.heightNorm}%`,
  } : isFit || isSplit ? null : cropWindowStyle(cropPath, activeSegment, elapsed)

  useEffect(() => {
    if (!isFit && !isSplit) {
      bgVideo.current?.pause()
      return
    }
    syncBackground(true)
  }, [isFit, isSplit, src, syncBackground])

  const [aspectW, aspectH] = ASPECTS[ratio] ?? ASPECTS['9:16']
  const sourceAspect = (cropPath?.source_width || 1) / (cropPath?.source_height || 1)
  const panelWidth = Math.min(1, sourceAspect * aspectH / (4 * aspectW))
  const panelHeight = panelWidth * 2 * aspectW / (sourceAspect * aspectH)
  const panelLeft = (1 - panelWidth) / 2
  const panelTop = (0.5 - panelHeight) / 2
  const splitVideoStyle = {
    width: `${panelWidth * 200}%`, height: `${panelHeight * 100}%`,
    left: `${panelLeft * 100}%`, top: `${panelTop * 100}%`,
    clipPath: 'inset(0 50% 0 0)',
  }
  // Height alone can't bound the box: with width:100% and an aspect-ratio, a
  // max-height clamp shortens the element without narrowing it, so the rendered
  // shape stops matching the ratio — a 9:16 preview ends up looking square.
  // Deriving a matching max-width makes the box shrink along both axes instead.
  const maxWidth = `${((MAX_HEIGHT_VH * aspectW) / aspectH).toFixed(3)}vh`

  return (
    <div className="mx-auto flex w-full flex-col items-stretch" style={{ maxWidth }}>
      <div
        className={`relative w-full overflow-hidden ${isSplit ? 'bg-black' : 'bg-ink-850'}`}
        onClick={() => {
          if (suppressClick.current) { suppressClick.current = false; return }
          toggle()
        }}
        onPointerDown={(event) => {
          if (!draftGeometry || !onPan || event.button !== 0) return
          drag.current = { x: event.clientX, y: event.clientY, moved: false }
          event.currentTarget.setPointerCapture(event.pointerId)
        }}
        onPointerMove={(event) => {
          if (!drag.current || !onPan) return
          const rect = event.currentTarget.getBoundingClientRect()
          if (!rect.width || !rect.height) return
          const dx = event.clientX - drag.current.x
          const dy = event.clientY - drag.current.y
          if (!drag.current.moved && Math.abs(dx) + Math.abs(dy) < 3) return
          drag.current = { x: event.clientX, y: event.clientY, moved: true }
          onPan(dx / rect.width, dy / rect.height)
        }}
        onPointerUp={() => {
          suppressClick.current = Boolean(drag.current?.moved)
          drag.current = null
        }}
        onPointerCancel={() => { drag.current = null }}
        data-testid="clip-preview"
        {...(draftGeometry ? { 'data-editing-framing': 'true' } : {})}
        style={{ aspectRatio: `${aspectW} / ${aspectH}`, containerType: 'size', touchAction: draftGeometry ? 'none' : undefined }}
        role="button"
        tabIndex={0}
        aria-label={playing ? 'Pause' : 'Play'}
        onKeyDown={(e) => {
          if (e.key === ' ' || e.key === 'Enter') {
            e.preventDefault()
            toggle()
          }
        }}
      >
        {(isFit || isSplit) && (
          <video
            ref={bgVideo}
            src={src}
            muted
            playsInline
            preload="auto"
            tabIndex={-1}
            aria-hidden="true"
            onLoadedMetadata={() => syncBackground(true)}
            className={isSplit ? 'pointer-events-none absolute max-w-none' : 'pointer-events-none absolute inset-0 size-full scale-110 object-cover blur-xl'}
            style={isSplit ? { ...splitVideoStyle, left: `${(panelLeft - panelWidth) * 100}%`, top: `${(0.5 + panelTop) * 100}%`, clipPath: 'inset(0 0 0 50%)' } : undefined}
          />
        )}

        <video
          ref={video}
          src={src}
          className={
            isSplit
              ? 'absolute max-w-none'
              : isFit
              ? 'relative z-10 size-full object-contain'
              : cropStyle
                ? 'absolute max-w-none'
                : 'size-full object-cover'
          }
          style={isSplit ? splitVideoStyle : cropStyle ?? undefined}
          onTimeUpdate={onTimeUpdate}
          onPlay={() => {
            setPlaying(true)
            syncBackground(true)
          }}
          onPause={() => {
            setPlaying(false)
            bgVideo.current?.pause()
          }}
          onSeeked={() => {
            if (video.current) updateTime(video.current.currentTime)
            syncBackground(true)
          }}
          onRateChange={() => syncBackground(false)}
          preload="auto"
          playsInline
          controls={nativeControls}
        />

        {burnCaptions && (
          <CaptionOverlay
            words={words}
            time={time}
            style={style}
            captionOverrides={captionOverrides}
          />
        )}

        {!playing && (
          <div className="pointer-events-none absolute inset-0 z-20 grid place-items-center">
            <span className="grid size-16 place-items-center rounded-full bg-ink-900/70 pl-1 text-2xl text-ink-100 backdrop-blur-[2px]">
              ▶
            </span>
          </div>
        )}
      </div>

      <div className="mt-3 flex w-full items-center gap-4">
        <button onClick={toggle} className="btn btn-quiet -ml-1 w-14 justify-start">
          {playing ? 'Pause' : 'Play'}
        </button>

        <VolumeControl
          volume={volume}
          muted={muted}
          onVolume={(next) => {
            setVolume(next)
            // Dragging the slider up is an unambiguous "I want to hear this".
            if (next > 0) setMuted(false)
          }}
          onToggleMute={() => setMuted((current) => !current)}
        />

        <div className="flex-1" />
        <span className="numeric text-xs text-ink-500">
          {formatTimecode(elapsed)} / {formatTimecode(duration)}
        </span>
      </div>

      {(muted || volume === 0) && (
        <p className="mt-2 text-xs text-sodium-500">
          Audio is muted — click the speaker to unmute.
        </p>
      )}

      <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1">
        <button onClick={runAudioCheck} disabled={checking} className="btn btn-quiet -ml-1">
          {checking ? 'Listening…' : 'Test audio'}
        </button>
        <button
          onClick={() => setNativeControls((current) => !current)}
          className="btn btn-quiet"
        >
          {nativeControls ? 'Hide browser controls' : 'Browser controls'}
        </button>
      </div>

      {audioCheck && (
        <p
          className={`mt-1 max-w-prose text-xs leading-relaxed ${
            audioCheck.ok ? 'text-ink-400' : 'text-sodium-500'
          }`}
        >
          {audioCheck.detail}
        </p>
      )}
    </div>
  )
}

interface AudioCheck {
  ok: boolean
  peakDb: number | null
  detail: string
}

/**
 * Measure the real signal level leaving the video element.
 *
 * "Is it muted?" is otherwise unanswerable from inside the page: a muted tab, a
 * silenced app in the OS mixer, and audio routed to a disconnected output all
 * look identical, and none of them are distinguishable from a bug in here.
 *
 * ``captureStream`` taps the element's output rather than rerouting it, so
 * measuring cannot itself cause the silence being investigated. The analyser is
 * deliberately never connected to the context destination — doing so would play
 * the audio a second time.
 */
async function measureOutputLevel(element: HTMLVideoElement): Promise<AudioCheck> {
  const capture =
    (element as HTMLVideoElement & { captureStream?: () => MediaStream }).captureStream ??
    (element as HTMLVideoElement & { mozCaptureStream?: () => MediaStream }).mozCaptureStream

  if (!capture) {
    return {
      ok: false,
      peakDb: null,
      detail: "This browser can't measure audio output. Try the browser controls instead.",
    }
  }

  const wasPaused = element.paused
  if (wasPaused) await element.play()

  const stream = capture.call(element)
  const tracks = stream.getAudioTracks()
  if (tracks.length === 0) {
    if (wasPaused) element.pause()
    return {
      ok: false,
      peakDb: null,
      detail: 'This video exposes no audio track at all — that is a problem in AutoClip.',
    }
  }

  const context = new AudioContext()
  const analyser = context.createAnalyser()
  analyser.fftSize = 2048
  context.createMediaStreamSource(stream).connect(analyser)

  const samples = new Float32Array(analyser.fftSize)
  let peak = 0
  for (let i = 0; i < 24; i += 1) {
    await new Promise((resolve) => setTimeout(resolve, 50))
    analyser.getFloatTimeDomainData(samples)
    for (const sample of samples) peak = Math.max(peak, Math.abs(sample))
  }

  await context.close()
  if (wasPaused) element.pause()

  if (peak <= 0.005) {
    return {
      ok: false,
      peakDb: null,
      detail:
        'No signal is leaving the player. Either the clip really is silent, or the ' +
        'player is muted — check the speaker icon above.',
    }
  }

  const peakDb = 20 * Math.log10(peak)
  return {
    ok: true,
    peakDb,
    detail:
      `Audio is leaving the player at ${peakDb.toFixed(1)} dBFS — the app is producing sound. ` +
      'If you still hear nothing, it is between the browser and your speakers: right-click ' +
      'this tab and check for "Unmute site", then check Windows Volume Mixer and your ' +
      'output device.',
  }
}

function activeCropSegment(
  cropPath: CropPath | null | undefined,
  elapsed: number,
): CropSegment | null {
  if (!cropPath || cropPath.segments.length === 0) return null
  return (
    cropPath.segments.find((s) => elapsed >= s.start_s && elapsed < s.end_s) ??
    cropPath.segments[cropPath.segments.length - 1]
  )
}

/**
 * Position the source video so the preview box shows the crop the renderer will.
 *
 * Everything is expressed as a percentage of the crop window, which makes it
 * independent of how large the preview happens to be drawn. Returns null when
 * there is no crop path — the caller then falls back to a centre crop, which is
 * what the renderer does in that case too.
 */
function cropWindowStyle(
  cropPath: CropPath | null | undefined,
  segment: CropSegment | null,
  elapsed: number,
): React.CSSProperties | null {
  if (!cropPath || !segment) return null

  // A fitted segment shows the whole frame over a blur rather than cropping.
  // Letting it fall through to object-contain is closer than any crop would be.
  if (segment.fit) return null

  const { x, y } = interpolate(segment.keyframes, elapsed)
  const { source_width: sourceW, source_height: sourceH } = cropPath

  return {
    width: `${(sourceW / segment.width) * 100}%`,
    height: `${(sourceH / segment.height) * 100}%`,
    left: `${(-x / segment.width) * 100}%`,
    top: `${(-y / segment.height) * 100}%`,
  }
}

/** Piecewise-linear lookup, matching croppath.axis_expression on the server. */
function interpolate(
  keyframes: { t: number; x: number; y: number }[],
  t: number,
): { x: number; y: number } {
  if (keyframes.length === 0) return { x: 0, y: 0 }
  if (keyframes.length === 1) return { x: keyframes[0].x, y: keyframes[0].y }

  if (t <= keyframes[0].t) return { x: keyframes[0].x, y: keyframes[0].y }
  const last = keyframes[keyframes.length - 1]
  if (t >= last.t) return { x: last.x, y: last.y }

  for (let i = 0; i < keyframes.length - 1; i += 1) {
    const a = keyframes[i]
    const b = keyframes[i + 1]
    if (t >= a.t && t <= b.t) {
      const span = b.t - a.t
      const ratio = span > 0 ? (t - a.t) / span : 0
      return { x: a.x + (b.x - a.x) * ratio, y: a.y + (b.y - a.y) * ratio }
    }
  }

  return { x: last.x, y: last.y }
}

/**
 * Mute toggle and volume slider.
 *
 * Not optional chrome: without it there is no way to see whether the preview is
 * silent because it's muted or because something upstream is wrong, and no way
 * to do anything about it. The slider stays visible rather than hiding behind a
 * hover, because "is this thing muted?" is a question you ask at a glance.
 */
function VolumeControl({
  volume,
  muted,
  onVolume,
  onToggleMute,
}: {
  volume: number
  muted: boolean
  onVolume: (value: number) => void
  onToggleMute: () => void
}) {
  const silent = muted || volume === 0

  return (
    <div className="flex items-center gap-2">
      <button
        onClick={onToggleMute}
        className={`btn btn-quiet px-1 ${silent ? 'text-sodium-500' : ''}`}
        aria-label={silent ? 'Unmute' : 'Mute'}
        title={silent ? 'Unmute' : 'Mute'}
      >
        <SpeakerIcon silent={silent} level={volume} />
      </button>
      <input
        type="range"
        min={0}
        max={1}
        step={0.05}
        value={silent ? 0 : volume}
        onChange={(e) => onVolume(Number(e.target.value))}
        aria-label="Volume"
        className="h-1 w-20 cursor-pointer appearance-none rounded-full bg-ink-700 accent-sodium-500"
      />
    </div>
  )
}

function SpeakerIcon({ silent, level }: { silent: boolean; level: number }) {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden>
      <path
        d="M11 5 6 9H3v6h3l5 4V5Z"
        fill="currentColor"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinejoin="round"
      />
      {silent ? (
        <path
          d="m16 9 5 6m0-6-5 6"
          stroke="currentColor"
          strokeWidth="1.8"
          strokeLinecap="round"
        />
      ) : (
        <>
          <path
            d="M15.5 9.5a3.5 3.5 0 0 1 0 5"
            stroke="currentColor"
            strokeWidth="1.6"
            strokeLinecap="round"
          />
          {level > 0.5 && (
            <path
              d="M18.5 7a7 7 0 0 1 0 10"
              stroke="currentColor"
              strokeWidth="1.6"
              strokeLinecap="round"
            />
          )}
        </>
      )}
    </svg>
  )
}

/** Group words the way the ASS generator does, then show the active group. */
function CaptionOverlay({
  words,
  time,
  style,
  captionOverrides,
}: {
  words: Word[]
  time: number
  style: CaptionStyle | undefined
  captionOverrides?: CaptionOverrides | null
}) {
  if (!style || words.length === 0) return null

  const groups = groupWords(words, style.preview.maxWords)
  const active = groups.find((group) => time >= group[0].start && time <= group[group.length - 1].end)
  if (!active) return null

  const {
    primary,
    accent,
    allCaps,
    outlineWidth,
    boxed,
    marginRatio,
    sizeRatio,
    positionX,
    positionY,
  } = style.preview

  const hasSizeOverride = Number.isFinite(captionOverrides?.size_ratio)
  const resolvedSizeRatio = hasSizeOverride ? captionOverrides!.size_ratio! : sizeRatio
  const resolvedX = Number.isFinite(captionOverrides?.position_x)
    ? captionOverrides!.position_x!
    : Number.isFinite(positionX)
      ? positionX!
      : null
  const resolvedY = Number.isFinite(captionOverrides?.position_y)
    ? captionOverrides!.position_y!
    : Number.isFinite(positionY)
      ? positionY!
      : null
  const useBottomCenterAnchor = resolvedX !== null || resolvedY !== null
  const anchorX = resolvedX ?? 0.5
  const anchorY = resolvedY ?? 1 - marginRatio

  return (
    <div
      className={
        useBottomCenterAnchor
          ? 'pointer-events-none absolute z-20 flex w-[94%] justify-center'
          : 'pointer-events-none absolute inset-x-0 z-20 flex justify-center px-[6%]'
      }
      style={
        useBottomCenterAnchor
          ? {
              left: `${anchorX * 100}%`,
              top: `${anchorY * 100}%`,
              transform: 'translate(-50%, -100%)',
            }
          : { bottom: `${marginRatio * 100}%` }
      }
    >
      <p
        className="min-w-0 wrap-anywhere text-center leading-[1.15]"
        style={{
          fontFamily: style.preview.font === 'Anton' ? 'Anton, Impact, sans-serif' : undefined,
          fontSize: hasSizeOverride
            ? `${resolvedSizeRatio * 100}cqh`
            : `clamp(0.75rem, ${resolvedSizeRatio * 100}cqh, 4rem)`,
          fontWeight: boxed || style.preview.font === 'Anton' ? 400 : 600,
          textTransform: allCaps ? 'uppercase' : 'none',
          color: primary,
          textShadow: boxed
            ? undefined
            : `0 0 ${outlineWidth}px #000, 0 0 ${outlineWidth * 2}px #000`,
          background: boxed ? 'rgba(0,0,0,0.78)' : undefined,
          padding: boxed ? '0.15em 0.4em' : undefined,
        }}
      >
        {active.map((word, index) => {
          const isActive = time >= word.start && time <= word.end
          return (
            <span
              key={`${word.start}-${index}`}
              style={{
                color: isActive && accent ? accent : undefined,
                display: 'inline-block',
                transform: isActive && accent ? 'scale(1.08)' : undefined,
                transition: 'transform 120ms cubic-bezier(0.16,1,0.3,1)',
                marginInline: '0.14em',
              }}
            >
              {allCaps ? word.text.toUpperCase() : word.text}
            </span>
          )
        })}
      </p>
    </div>
  )
}

function groupWords(words: Word[], maxWords: number): Word[][] {
  const groups: Word[][] = []
  let current: Word[] = []

  for (const [index, word] of words.entries()) {
    if (current.length > 0) {
      const gap = word.start - current[current.length - 1].end
      if (gap > 0.4 || current.length >= maxWords) {
        groups.push(current)
        current = []
      }
    }
    current.push(word)
    if (/[.!?…]$/.test(word.text.trim()) && index !== words.length - 1) {
      groups.push(current)
      current = []
    }
  }
  if (current.length > 0) groups.push(current)
  return groups
}
