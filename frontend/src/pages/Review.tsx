import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useBeforeUnload, useBlocker, useNavigate, useParams } from 'react-router-dom'

import {
  api,
  formatBytes,
  formatDuration,
  type CaptionOverrides,
  type CaptionStyle,
  type Clip,
  type ClipEditPatch,
  type ClipFraming,
  type CropPath,
  type Job,
  type ManualFraming,
  type SingleFraming,
  type SlicesFraming,
  type Word,
} from '../api'
import { CaptionEditor } from '../components/CaptionEditor'
import { CaptionLayoutEditor } from '../components/CaptionLayoutEditor'
import { ClipPlayer, type FramingGeometry, type PanRequest } from '../components/ClipPlayer'
import { ErrorNote } from '../components/ErrorNote'
import { ExactPreview } from '../components/ExactPreview'
import { SliceFramingEditor } from '../components/SliceFramingEditor'

const RATIOS = ['9:16', '1:1', '16:9'] as const

function canonicalCaptionOverridesKey(overrides: CaptionOverrides | null | undefined): string {
  if (!overrides) return 'null'
  return JSON.stringify({
    size_ratio: overrides.size_ratio ?? null,
    position_x: overrides.position_x ?? null,
    position_y: overrides.position_y ?? null,
  })
}

function canonicalFramingKey(framing: ClipFraming | undefined): string {
  return JSON.stringify(framing ?? null)
}

export function Review() {
  const { jobId, clipId } = useParams()
  const navigate = useNavigate()
  const [job, setJob] = useState<Job | null>(null)
  const [clips, setClips] = useState<Clip[]>([])
  const [clipsLoaded, setClipsLoaded] = useState(false)
  const [styles, setStyles] = useState<CaptionStyle[]>([])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [words, setWords] = useState<Word[]>([])
  const [cropPath, setCropPath] = useState<CropPath | null>(null)
  const [playheadSourceS, setPlayheadSourceS] = useState<number>(0)
  const [seekRequest, setSeekRequest] = useState<{ time: number; id: number } | null>(null)
  const [draftGeometry, setDraftGeometry] = useState<FramingGeometry | null>(null)
  const [panRequest, setPanRequest] = useState<PanRequest | null>(null)
  const [wordsDirty, setWordsDirty] = useState(false)
  const [framingDirty, setFramingDirty] = useState(false)
  const [captionLayoutDirty, setCaptionLayoutDirty] = useState(false)
  const [savingWords, setSavingWords] = useState(false)
  const [wordsRevision, setWordsRevision] = useState(0)
  const [savingSettings, setSavingSettings] = useState(false)
  const [trimming, setTrimming] = useState(false)
  const [dirtyBlockedMessage, setDirtyBlockedMessage] = useState<string | null>(null)
  const [exporting, setExporting] = useState<Set<string>>(new Set())
  const [exportPending, setExportPending] = useState(false)
  const exportLock = useRef(false)
  const [error, setError] = useState<Error | null>(null)
  const selectedIdRef = useRef<string | null>(null)
  selectedIdRef.current = selectedId

  useEffect(() => {
    if (!jobId) return
    setClipsLoaded(false)
    void Promise.all([api.getJob(jobId), api.listClips(jobId), api.captionStyles()])
      .then(([loadedJob, loadedClips, loadedStyles]) => {
        setJob(loadedJob)
        setClips(loadedClips)
        setStyles(loadedStyles)
        setSelectedId(clipId ?? loadedClips[0]?.id ?? null)
        setClipsLoaded(true)
      })
      .catch((err) => setError(err as Error))
  }, [jobId])

  useEffect(() => {
    if (clipId && clips.some((clip) => clip.id === clipId)) {
      setSelectedId(clipId)
    }
  }, [clipId, clips, clipsLoaded, jobId, navigate])

  const selected = useMemo(
    () => clips.find((clip) => clip.id === selectedId) ?? null,
    [clips, selectedId],
  )

  const selectedClipId = selected?.id ?? null
  const selectedRatio = selected?.ratio ?? null
  const selectedFraming = selected?.framing ?? null
  const selectedFramingKey = canonicalFramingKey(selectedFraming)
  const selectedManualFraming = selectedFraming?.mode === 'manual' ? selectedFraming : null
  const selectedSlicesFraming = selectedFraming?.mode === 'slices' ? selectedFraming : null
  const timelineFraming = useMemo((): SlicesFraming | null => selected
    ? selectedSlicesFraming ?? {
        mode: 'slices',
        segments: [{
          start_s: selected.start_s,
          end_s: selected.end_s,
          framing: selectedFraming?.mode === 'manual' || selectedFraming?.mode === 'fit' || selectedFraming?.mode === 'split'
            ? selectedFraming
            : { mode: 'auto' },
        }],
      }
    : null, [selected, selectedSlicesFraming, selectedFraming])
  const selectedStartS = selected?.start_s ?? null
  const selectedEndS = selected?.end_s ?? null

  useEffect(() => {
    if (!selectedClipId) {
      setWords([])
      return
    }
    let cancelled = false
    setWordsDirty(false)
    setFramingDirty(false)
    setCaptionLayoutDirty(false)
    setDirtyBlockedMessage(null)
    setWords([])
    if (selectedStartS !== null) setPlayheadSourceS(selectedStartS)
    api
      .getClipWords(selectedClipId)
      .then((nextWords) => {
        if (!cancelled && selectedIdRef.current === selectedClipId) {
          setWords(nextWords)
        }
      })
      .catch(() => {
        if (!cancelled && selectedIdRef.current === selectedClipId) {
          setWords([])
        }
      })
    return () => {
      cancelled = true
    }
  }, [selectedClipId])

  useEffect(() => {
    if (!selectedClipId) {
      setCropPath(null)
      return
    }
    let cancelled = false
    setCropPath(null)
    // 404 is expected for audio-only sources and jobs that never reframed; the
    // player falls back to a centre crop, matching what the renderer does.
    api
      .getCropPath(selectedClipId)
      .then((nextCropPath) => {
        if (!cancelled && selectedIdRef.current === selectedClipId) {
          setCropPath(nextCropPath)
        }
      })
      .catch(() => {
        if (!cancelled && selectedIdRef.current === selectedClipId) {
          setCropPath(null)
        }
      })
    return () => {
      cancelled = true
    }
  }, [
    selectedClipId,
    selectedRatio,
    selectedFramingKey,
    selectedStartS,
    selectedEndS,
  ])

  const patchClip = useCallback((updated: Clip) => {
    setClips((current) => current.map((clip) => (clip.id === updated.id ? updated : clip)))
  }, [])

  const editsPending = savingWords || savingSettings || trimming
  const controlsLocked = editsPending || exportPending
  const guardDirtyCaptions = (actionLabel: string): boolean => {
    if (framingDirty) {
      setDirtyBlockedMessage(`Apply or discard framing changes before ${actionLabel}.`)
      return true
    }
    if (captionLayoutDirty) {
      setDirtyBlockedMessage(
        `Apply or discard caption layout changes before ${actionLabel}.`,
      )
      return true
    }
    if (!wordsDirty) return false
    setDirtyBlockedMessage(
      `Save caption changes before ${actionLabel} so your edited words are not lost.`,
    )
    return true
  }
  const guardCaptionLayoutDraft = (actionLabel: string): boolean => {
    if (!captionLayoutDirty) return false
    setDirtyBlockedMessage(`Apply or discard caption layout changes before ${actionLabel}.`)
    return true
  }

  const selectClip = (clipId: string) => {
    if (clipId === selectedId) return
    if (editsPending || exportLock.current) return
    if (guardDirtyCaptions('switching clips')) return
    setDirtyBlockedMessage(null)
    navigate(`/jobs/${jobId}/clips/${clipId}`)
  }

  const setStatus = async (clip: Clip, status: Clip['status']) => {
    if (editsPending || exportLock.current) return
    try {
      patchClip(await api.patchClip(clip.id, { status }))
    } catch (err) {
      setError(err as Error)
    }
  }

  const commitTrim = async (start: number, end: number) => {
    if (!selected || editsPending || exportLock.current) return
    if (guardDirtyCaptions('trimming the clip')) return
    const clipId = selected.id
    setTrimming(true)
    setError(null)
    try {
      patchClip(await api.patchClip(clipId, { start_s: start, end_s: end }))
      const nextWords = await api.getClipWords(clipId)
      if (selectedIdRef.current === clipId) {
        setWords(nextWords)
      }
    } catch (err) {
      setError(err as Error)
    } finally {
      setTrimming(false)
    }
  }

  const saveWords = async () => {
    if (!selected || editsPending || exportLock.current) return
    const clipId = selected.id
    const wordsSnapshot = words
    setSavingWords(true)
    setError(null)
    try {
      patchClip(await api.patchEdit(clipId, { words: wordsSnapshot }))
      if (selectedIdRef.current === clipId) {
        setWordsDirty(false)
        setDirtyBlockedMessage(null)
        setWordsRevision((rev) => rev + 1)
      }
    } catch (err) {
      setError(err as Error)
    } finally {
      setSavingWords(false)
    }
  }

  const updateClipEdit = async (clip: Clip, patch: ClipEditPatch): Promise<boolean> => {
    if (editsPending || exportLock.current) return false
    setSavingSettings(true)
    setError(null)
    try {
      patchClip(await api.patchEdit(clip.id, patch))
      return true
    } catch (err) {
      setError(err as Error)
      return false
    } finally {
      setSavingSettings(false)
    }
  }

  const setStyle = async (clip: Clip, styleKey: string) => {
    if (guardCaptionLayoutDraft('changing caption style')) return
    await updateClipEdit(clip, { caption_style: styleKey, caption_overrides: null })
  }

  const setRatio = async (clip: Clip, ratio: string) => {
    if (guardDirtyCaptions('changing aspect ratio')) return
    await updateClipEdit(clip, { ratio })
  }

  const setBurnCaptions = async (clip: Clip, burnCaptions: boolean) => {
    if (guardCaptionLayoutDraft('toggling subtitles')) return
    await updateClipEdit(clip, { burn_captions: burnCaptions })
  }

  const setFraming = async (clip: Clip, framing: ClipFraming): Promise<boolean> => {
    if (guardDirtyCaptions('changing framing mode')) return false
    return updateClipEdit(clip, { framing })
  }

  const commitSlicesFraming = async (clip: Clip, nextSlices: SlicesFraming): Promise<boolean> => {
    if (guardCaptionLayoutDraft('saving slice framing')) return false
    if (wordsDirty) {
      setDirtyBlockedMessage('Save caption changes before saving slice framing.')
      return false
    }
    if (clip.framing?.mode !== 'slices' && nextSlices.segments.length === 1) {
      const framing = nextSlices.segments[0].framing
      return updateClipEdit(clip, { framing: framing.mode === 'auto' ? null : framing })
    }
    return updateClipEdit(clip, { framing: nextSlices })
  }

  const enableSlicesFraming = async (clip: Clip): Promise<boolean> => {
    if (guardDirtyCaptions('enabling per-slice framing')) return false
    if (clip.framing?.mode === 'slices') return true
    let initialFraming: SingleFraming = { mode: 'auto' }
    if (clip.framing?.mode === 'fit' || clip.framing?.mode === 'split') {
      initialFraming = { mode: clip.framing.mode }
    } else if (clip.framing?.mode === 'manual') {
      initialFraming = { ...clip.framing }
    }
    const payload: SlicesFraming = {
      mode: 'slices',
      segments: [
        {
          start_s: clip.start_s,
          end_s: clip.end_s,
          framing: initialFraming,
        },
      ],
    }
    return updateClipEdit(clip, { framing: payload })
  }

  const setCaptionOverrides = async (
    clip: Clip,
    captionOverrides: CaptionOverrides | null,
  ): Promise<boolean> => {
    return updateClipEdit(clip, { caption_overrides: captionOverrides })
  }

  const exportClips = async (targets: Clip[]) => {
    if (editsPending || exportLock.current) return
    if (guardDirtyCaptions('exporting')) return
    exportLock.current = true
    setExportPending(true)
    setError(null)
    try {
      for (const clip of targets) {
        setExporting(new Set([clip.id]))
        await api.exportClip(clip.id)
        patchClip(await api.getClip(clip.id))
      }
    } catch (err) {
      setError(err as Error)
    } finally {
      setExporting(new Set())
      exportLock.current = false
      setExportPending(false)
    }
  }

  const exportKept = async () => {
    await exportClips(clips.filter((clip) => clip.status === 'kept'))
  }

  const keptCount = clips.filter((clip) => clip.status === 'kept').length
  const activeStyle = styles.find((style) => style.key === selected?.caption_style)
  const burnCaptions = selected?.burn_captions ?? true
  const framingMode = selected?.framing?.mode ?? 'auto'
  const hasVideoSource = Boolean(
    job?.source?.has_video &&
      ((job.source.width && job.source.height) ||
        (cropPath && cropPath.source_width > 0 && cropPath.source_height > 0)),
  )
  const defaultManualFraming: ManualFraming = { mode: 'manual', center_x: 0.5, center_y: 0.5, zoom: 1 }
  const savedPreviewSignature = selected
    ? JSON.stringify({
        id: selected.id,
        start_s: selected.start_s,
        end_s: selected.end_s,
        ratio: selected.ratio,
        caption_style: selected.caption_style,
        burn_captions: selected.burn_captions ?? true,
        caption_overrides: canonicalCaptionOverridesKey(selected.caption_overrides),
        framing: selectedFramingKey,
        wordsRevision,
      })
    : ''
  const hasUnsavedDrafts = wordsDirty || framingDirty || captionLayoutDirty
  const blocker = useBlocker(hasUnsavedDrafts || editsPending || exportPending)
  useBeforeUnload((event) => {
    if (!hasUnsavedDrafts && !editsPending && !exportPending) return
    event.preventDefault()
    event.returnValue = ''
  })
  const previewDisabled = controlsLocked || hasUnsavedDrafts
  const previewDisabledReason = hasUnsavedDrafts
    ? 'Save or discard unsaved caption, layout, or framing changes before rendering an exact preview.'
    : null

  if (error && clips.length === 0) {
    return (
      <div className="max-w-3xl pt-24">
        <ErrorNote error={error} />
      </div>
    )
  }

  if (clipId && clipsLoaded && !clips.some((clip) => clip.id === clipId) && job) {
    return (
      <div className="max-w-2xl pt-16">
        <p className="eyebrow">Clip unavailable</p>
        <h1 className="mt-2 font-display text-3xl text-ink-100">Clip not found</h1>
        <p className="mt-3 text-sm text-ink-300">This clip does not belong to this job or no longer exists.</p>
        <Link className="btn btn-ghost mt-6" to={`/jobs/${jobId}/clips`}>Back to results</Link>
      </div>
    )
  }

  if (!job) return <p className="pt-24 text-sm text-ink-500">Loading…</p>

  return (
    <div className="pt-10">
      <div className="flex flex-wrap items-baseline justify-between gap-4 border-b border-ink-800 pb-5">
        <div className="min-w-0">
          <Link to={`/jobs/${jobId}/clips`} className="eyebrow transition-colors hover:text-sodium-500">
            ← Results
          </Link>
          <h1 className="mt-2 max-w-2xl truncate font-display text-[clamp(1.5rem,3vw,2.25rem)] leading-tight text-ink-100">
            {job.source?.title || 'Untitled'}
          </h1>
        </div>

        <div className="flex items-baseline gap-6">
          <span className="numeric text-xs text-ink-500">
            {clips.length} clips · {keptCount} kept
          </span>
          <button
            onClick={exportKept}
            disabled={
              keptCount === 0 ||
              controlsLocked ||
              wordsDirty ||
              framingDirty ||
              captionLayoutDirty
            }
            className="btn btn-primary"
          >
            Export kept
          </button>
        </div>
      </div>

      {error && (
        <div className="mt-6 max-w-3xl">
          <ErrorNote error={error} onDismiss={() => setError(null)} />
        </div>
      )}

      {blocker.state === 'blocked' && (
        <div role="alert" className="mt-4 flex flex-wrap items-center gap-3 border-l-2 border-sodium-500 bg-ink-850 p-3 text-sm text-ink-100">
          <span>{editsPending || exportPending ? 'Wait for the current save or export to finish before leaving.' : 'Unsaved edits will be lost if you leave this clip.'}</span>
          <button className="btn btn-ghost" onClick={() => blocker.reset()}>Stay</button>
          {!editsPending && !exportPending && <button className="btn btn-primary" onClick={() => blocker.proceed()}>Leave without saving</button>}
        </div>
      )}

      {(dirtyBlockedMessage || wordsDirty || framingDirty || captionLayoutDirty) && (
        <p
          role="alert"
          className="mt-4 max-w-3xl border-l-2 border-sodium-500 bg-ink-850/70 py-2 pl-3 pr-4 text-xs text-sodium-500"
        >
          {dirtyBlockedMessage || (framingDirty
            ? 'Apply or discard framing changes before switching clips, trimming, changing ratio or mode, or exporting.'
            : captionLayoutDirty
              ? 'Apply or discard caption layout changes before switching clips, trimming, changing style, ratio, or mode, or exporting.'
              : 'Save caption changes before trimming or exporting.')}
        </p>
      )}

      {clips.length === 0 ? (
        <EmptyState />
      ) : (
        <div className="mt-8 grid gap-x-6 gap-y-8 xl:grid-cols-[minmax(0,14rem)_minmax(0,1fr)_minmax(0,20rem)]">
          {/* Ranked list */}
          <section className="lg:max-h-[76vh] lg:overflow-y-auto lg:pr-1">
            <p className="eyebrow border-b border-ink-800 pb-2">Ranked</p>
            <ul>
              {clips.map((clip, index) => (
                <li key={clip.id}>
                  <ClipRow
                    clip={clip}
                    index={index}
                    selected={clip.id === selectedId}
                    disabled={controlsLocked}
                    onSelect={() => selectClip(clip.id)}
                    onKeep={() =>
                      setStatus(clip, clip.status === 'kept' ? 'candidate' : 'kept')
                    }
                    onDiscard={() =>
                      setStatus(clip, clip.status === 'discarded' ? 'candidate' : 'discarded')
                    }
                  />
                </li>
              ))}
            </ul>
          </section>

          {/* Player + trim */}
          <section className="space-y-8">
            {selected && jobId && (
              <>
                <ClipPlayer
                  src={api.mediaUrl(jobId)}
                  startS={selected.start_s}
                  endS={selected.end_s}
                  words={words}
                  style={activeStyle}
                  ratio={selected.ratio}
                  cropPath={cropPath}
                  burnCaptions={burnCaptions}
                  captionOverrides={selected.caption_overrides}
                  onPlayheadChange={setPlayheadSourceS}
                  seekRequest={seekRequest}
                  draftGeometry={draftGeometry}
                  onPan={(dx, dy) => setPanRequest((previous) => ({ dx, dy, id: (previous?.id ?? 0) + 1 }))}
                />
                {timelineFraming && (
                  <SliceFramingEditor
                    clipId={selected.id}
                    clipStartS={selected.start_s}
                    clipEndS={selected.end_s}
                    ratio={selected.ratio}
                    slicesFraming={timelineFraming}
                    cropPath={cropPath}
                    sourceWidth={job.source?.width ?? cropPath?.source_width ?? 0}
                    sourceHeight={job.source?.height ?? cropPath?.source_height ?? 0}
                    hasVideoSource={hasVideoSource}
                    playheadSourceS={playheadSourceS}
                    onSeek={(time) => setSeekRequest((previous) => ({ time, id: (previous?.id ?? 0) + 1 }))}
                    disabled={controlsLocked || captionLayoutDirty || wordsDirty}
                    framingDirty={framingDirty}
                    onDirtyChange={(dirty) => {
                      if (selectedIdRef.current !== selected.id) return
                      setFramingDirty(dirty)
                      if (!dirty) setDirtyBlockedMessage(null)
                    }}
                    onCommitSlices={(nextSlices) => commitSlicesFraming(selected, nextSlices)}
                    words={words}
                    onTrim={commitTrim}
                    onDraftGeometry={setDraftGeometry}
                    panRequest={panRequest}
                  />
                )}
                <ExactPreview
                  clip={selected}
                  playheadSourceS={playheadSourceS}
                  signature={savedPreviewSignature}
                  disabled={previewDisabled}
                  disabledReason={previewDisabledReason}
                />
              </>
            )}
          </section>

          {/* Editor + style */}
          <section className="space-y-10">
            {selected && (
              <>
                <div>
                  <p className="eyebrow">Why this clip</p>
                  <p className="mt-2 max-w-prose text-[0.9375rem] leading-relaxed text-ink-300">
                    {selected.reason || 'No rationale returned for this clip.'}
                  </p>
                  {selected.hook && (
                    <p className="mt-3 border-l-2 border-ink-700 pl-3 font-display text-lg italic text-ink-200">
                      “{selected.hook}”
                    </p>
                  )}
                </div>

                <CaptionEditor
                  words={words}
                  onChange={(next) => {
                    if (controlsLocked || exportLock.current) return
                    setWords(next)
                    setWordsDirty(true)
                  }}
                  onSave={saveWords}
                  saving={savingWords}
                  disabled={controlsLocked}
                  dirty={wordsDirty}
                />

                <div>
                  <div className="flex items-center justify-between border-b border-ink-800 pb-2">
                    <p className="eyebrow">Caption presets</p>
                    <label className="flex cursor-pointer items-center gap-2 text-xs text-ink-200">
                      <input
                        type="checkbox"
                        checked={burnCaptions}
                        disabled={controlsLocked}
                        onChange={(e) => setBurnCaptions(selected, e.target.checked)}
                        className="accent-sodium-500"
                      />
                      <span>Subtitles</span>
                    </label>
                  </div>
                  {selected.caption_overrides && (
                    <p className="mt-2 text-xs text-sodium-500">
                      Custom layout active; choose a preset to restore its layout.
                    </p>
                  )}
                  <div className="mt-3 space-y-1">
                    {styles.map((style) => (
                      <button
                        key={style.key}
                        type="button"
                        disabled={controlsLocked}
                        onClick={() => setStyle(selected, style.key)}
                        className={[
                          'block w-full border-l-2 py-2 pl-3 text-left transition-colors duration-200',
                          style.key === selected.caption_style
                            ? 'border-sodium-500 bg-ink-850/60'
                            : 'border-transparent hover:border-ink-700 hover:bg-ink-850/30',
                        ].join(' ')}
                      >
                        <span className="text-sm text-ink-100">{style.label}</span>
                        <span className="mt-0.5 block text-xs leading-snug text-ink-500">
                          {style.description}
                        </span>
                      </button>
                    ))}
                  </div>
                </div>

                <details className="group border-t border-ink-800 pt-4">
                  <summary className="cursor-pointer select-none text-xs uppercase tracking-wider text-ink-400 hover:text-ink-200">
                    Advanced / legacy caption layout
                  </summary>
                  <div className="mt-4">
                    <CaptionLayoutEditor
                      key={`${selected.id}:${selected.caption_style}:${canonicalCaptionOverridesKey(selected.caption_overrides)}`}
                      clipId={selected.id}
                      style={activeStyle}
                      overrides={selected.caption_overrides}
                      disabled={controlsLocked}
                      onDirtyChange={(dirty) => {
                        if (selectedIdRef.current !== selected.id) return
                        setCaptionLayoutDirty(dirty)
                        if (!dirty) setDirtyBlockedMessage(null)
                      }}
                      onCommit={(nextOverrides) =>
                        setCaptionOverrides(selected, nextOverrides)
                      }
                    />
                  </div>
                </details>

                <div>
                  <p className="eyebrow border-b border-ink-800 pb-2">Aspect ratio</p>
                  <div className="mt-3 flex gap-2">
                    {RATIOS.map((ratio) => (
                      <button
                        key={ratio}
                        type="button"
                        disabled={controlsLocked}
                        onClick={() => setRatio(selected, ratio)}
                        className={[
                          'numeric btn',
                          ratio === selected.ratio ? 'btn-primary' : 'btn-ghost',
                        ].join(' ')}
                      >
                        {ratio}
                      </button>
                    ))}
                  </div>
                </div>

                <details className="border-t border-ink-800 pt-4">
                  <summary className="cursor-pointer text-xs uppercase tracking-wider text-ink-400">Whole clip framing</summary>
                  <div className="mt-3 flex flex-wrap gap-2">
                    <button
                      type="button"
                      disabled={controlsLocked}
                      onClick={() => setFraming(selected, null)}
                      className={[
                        'btn',
                        framingMode === 'auto' ? 'btn-primary' : 'btn-ghost',
                      ].join(' ')}
                    >
                      Auto
                    </button>
                    <button
                      type="button"
                      disabled={controlsLocked}
                      onClick={() => setFraming(selected, { mode: 'fit' })}
                      className={[
                        'btn',
                        framingMode === 'fit' ? 'btn-primary' : 'btn-ghost',
                      ].join(' ')}
                    >
                      Fit + blurred background
                    </button>
                    <button
                      type="button"
                      disabled={controlsLocked || !hasVideoSource}
                      onClick={() =>
                        setFraming(
                          selected,
                          selectedManualFraming ?? defaultManualFraming,
                        )
                      }
                      className={[
                        'btn',
                        framingMode === 'manual' ? 'btn-primary' : 'btn-ghost',
                      ].join(' ')}
                    >
                      Manual
                    </button>
                    <button
                      type="button"
                      disabled={controlsLocked}
                      onClick={() => void enableSlicesFraming(selected)}
                      className={[
                        'btn',
                        framingMode === 'slices' ? 'btn-primary' : 'btn-ghost',
                      ].join(' ')}
                    >
                      Per-slice framing
                    </button>
                  </div>
                  {framingMode === 'slices' && (
                    <p className="mt-2 text-xs text-ink-400">
                      Per-slice framing is active. Choosing Auto, Fit, or Manual above replaces all slices for this clip.
                    </p>
                  )}
                </details>

                <div className="border-t border-ink-800 pt-6">
                  <button
                    onClick={() => exportClips([selected])}
                    disabled={
                      controlsLocked || wordsDirty || framingDirty || captionLayoutDirty
                    }
                    className="btn btn-primary w-full"
                  >
                    {exporting.has(selected.id) ? 'Rendering…' : 'Export this clip'}
                  </button>

                  {selected.exports.length > 0 && (
                    <ul className="mt-4 space-y-2">
                      {selected.exports.map((record) => (
                        <li
                          key={record.id}
                          className="flex items-baseline justify-between gap-3 text-xs"
                        >
                          <a
                            href={record.download_url}
                            download
                            className="truncate text-sodium-500 underline underline-offset-4"
                          >
                            {record.ratio} · {record.style}
                          </a>
                          <span className="numeric shrink-0 text-ink-600">
                            {formatBytes(record.size_bytes)}
                          </span>
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              </>
            )}
          </section>
        </div>
      )}
    </div>
  )
}

function ClipRow({
  clip,
  index,
  selected,
  disabled,
  onSelect,
  onKeep,
  onDiscard,
}: {
  clip: Clip
  index: number
  selected: boolean
  disabled?: boolean
  onSelect: () => void
  onKeep: () => void
  onDiscard: () => void
}) {
  return (
    <div
      className={[
        'rise group grid cursor-pointer grid-cols-[2.25rem_1fr] gap-x-3 border-b border-ink-850 py-3 pl-2 transition-colors duration-200',
        selected ? 'bg-ink-850/70' : 'hover:bg-ink-850/35',
        clip.status === 'discarded' ? 'opacity-40' : '',
      ].join(' ')}
      style={{ animationDelay: `${Math.min(index, 10) * 35}ms` }}
      onClick={() => !disabled && onSelect()}
      role="button"
      tabIndex={disabled ? -1 : 0}
      aria-disabled={disabled || undefined}
      onKeyDown={(e) => {
        if (!disabled && e.key === 'Enter') onSelect()
      }}
    >
      {/* The score is the thing you scan by, so it gets the display face. */}
      <span
        className={[
          'numeric self-start font-display text-2xl leading-none',
          clip.score >= 85 ? 'text-sodium-500' : selected ? 'text-ink-200' : 'text-ink-500',
        ].join(' ')}
      >
        {clip.score}
      </span>

      <div className="min-w-0">
        <p className="truncate text-[0.9375rem] leading-snug text-ink-100">
          {clip.title || 'Untitled clip'}
        </p>
        <div className="mt-1 flex items-baseline gap-3">
          <span className="numeric text-xs text-ink-500">{formatDuration(clip.duration_s)}</span>
          {clip.user_trimmed && <span className="text-xs text-ink-600">trimmed</span>}
          {clip.exports.length > 0 && (
            <span className="text-xs text-signal-good">exported</span>
          )}

          {/* Secondary actions appear on hover or when the row is current —
              progressive disclosure keeps the scan column clean. */}
          <span
            className={[
              'ml-auto flex gap-3 pr-2 transition-opacity duration-200',
              selected ? 'opacity-100' : 'opacity-0 group-hover:opacity-100',
            ].join(' ')}
          >
            <button
              disabled={disabled}
              onClick={(e) => {
                e.stopPropagation()
                onKeep()
              }}
              className={`text-xs ${clip.status === 'kept' ? 'text-signal-good' : 'text-ink-400 hover:text-ink-100'}`}
            >
              {clip.status === 'kept' ? 'kept' : 'keep'}
            </button>
            <button
              disabled={disabled}
              onClick={(e) => {
                e.stopPropagation()
                onDiscard()
              }}
              className="text-xs text-ink-400 hover:text-signal-bad"
            >
              {clip.status === 'discarded' ? 'undo' : 'drop'}
            </button>
          </span>
        </div>
      </div>
    </div>
  )
}

function EmptyState() {
  return (
    <div className="max-w-xl pt-20">
      <h2 className="font-display text-3xl text-ink-200">No clips came back.</h2>
      <p className="mt-4 text-sm leading-relaxed text-ink-400">
        The model found nothing self-contained enough to stand alone — which is a real
        answer for some source material, not necessarily a failure.
      </p>
      <p className="mt-3 text-sm leading-relaxed text-ink-400">
        If you expected clips, try a larger provider model, or widen the length range so
        shorter moments qualify.
      </p>
    </div>
  )
}
