import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'

import {
  ApiError,
  api,
  formatBytes,
  formatDuration,
  type Job,
  type JobSettingsOverrides,
  type ProviderStatus,
  type Source,
  type UploadProgress,
} from '../api'
import { ErrorNote } from '../components/ErrorNote'
import { LibraryPicker } from '../components/LibraryPicker'

type BusyKind = 'url' | 'file' | 'existing' | 'library'
type FilePhase = 'uploading' | 'validating' | 'starting'

interface UploadTransferState {
  filename: string
  loaded: number
  total: number
  lengthComputable: boolean
  phase: FilePhase
  speedBps: number | null
  etaSeconds: number | null
}

function isAbortError(err: unknown): boolean {
  return (
    (err instanceof DOMException && err.name === 'AbortError') ||
    (err instanceof Error && err.name === 'AbortError')
  )
}

export function Ingest() {
  const navigate = useNavigate()
  const [url, setUrl] = useState('')
  const [busy, setBusy] = useState<BusyKind | null>(null)
  const busyRef = useRef(false)
  const [error, setError] = useState<ApiError | Error | null>(null)
  const [statusNote, setStatusNote] = useState<string | null>(null)
  const [uploadState, setUploadState] = useState<UploadTransferState | null>(null)
  const [jobs, setJobs] = useState<Job[]>([])
  const [sources, setSources] = useState<Source[]>([])
  const [sourcesLoading, setSourcesLoading] = useState(true)
  const [sourcesError, setSourcesError] = useState<string | null>(null)
  const [selectedSourceId, setSelectedSourceId] = useState('')
  const [providers, setProviders] = useState<ProviderStatus[]>([])
  const [overrides, setOverrides] = useState<JobSettingsOverrides>({})
  const [advancedOpen, setAdvancedOpen] = useState(false)
  const [dragging, setDragging] = useState(false)
  const fileInput = useRef<HTMLInputElement>(null)
  const uploadAbortRef = useRef<AbortController | null>(null)
  const mountedRef = useRef(true)
  const speedSampleRef = useRef<{ startedAt: number; lastAt: number; lastLoaded: number; emaBps: number | null } | null>(null)

  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
      uploadAbortRef.current?.abort()
      uploadAbortRef.current = null
    }
  }, [])

  const loadSources = useCallback(() => {
    setSourcesLoading(true)
    setSourcesError(null)
    api
      .listSources()
      .then((list) => {
        setSources(list)
        setSelectedSourceId((prev) =>
          prev && list.some((s) => s.id === prev) ? prev : (list[0]?.id ?? ''),
        )
      })
      .catch((err) => {
        setSourcesError(err instanceof Error ? err.message : 'Failed to load sources')
      })
      .finally(() => {
        setSourcesLoading(false)
      })
  }, [])

  useEffect(() => {
    api.listJobs(8).then(setJobs).catch(() => undefined)
    api.providerStatus().then(setProviders).catch(() => undefined)
    loadSources()
  }, [loadSources])

  const start = useCallback(
    async (kind: BusyKind, run: () => Promise<{ id: string }>) => {
      if (busyRef.current) return
      busyRef.current = true
      setBusy(kind)
      setError(null)
      setStatusNote(null)
      const jobOverrides = { ...overrides }
      try {
        const source = await run()
        if (!mountedRef.current) return
        const job = await api.createJob(source.id, jobOverrides)
        if (!mountedRef.current) return
        navigate(`/jobs/${job.id}`)
      } catch (err) {
        if (!mountedRef.current) return
        setError(err as Error)
      } finally {
        busyRef.current = false
        if (mountedRef.current) {
          setBusy(null)
        }
      }
    },
    [navigate, overrides],
  )

  const submitUrl = (event: React.FormEvent) => {
    event.preventDefault()
    if (!url.trim()) return
    void start('url', () => api.ingestYouTube(url.trim()))
  }

  const submitFile = (file: File) => {
    if (busyRef.current) return
    busyRef.current = true
    setBusy('file')
    setError(null)
    setStatusNote(null)

    const jobOverrides = { ...overrides }
    const controller = new AbortController()
    uploadAbortRef.current = controller
    const now = performance.now()
    speedSampleRef.current = {
      startedAt: now,
      lastAt: now,
      lastLoaded: 0,
      emaBps: null,
    }

    setUploadState({
      filename: file.name,
      loaded: 0,
      total: file.size,
      lengthComputable: file.size > 0,
      phase: 'uploading',
      speedBps: null,
      etaSeconds: null,
    })

    const handleProgress = (prog: UploadProgress) => {
      if (!mountedRef.current || controller.signal.aborted) return
      const timestamp = performance.now()
      const sample = speedSampleRef.current
      let speedBps: number | null = null
      let etaSeconds: number | null = null

      if (sample && !prog.uploadComplete) {
        const dt = (timestamp - sample.lastAt) / 1000
        const dBytes = prog.loaded - sample.lastLoaded
        if (dt >= 0.15 && dBytes >= 0) {
          const instantBps = dBytes / dt
          sample.emaBps =
            sample.emaBps === null ? instantBps : sample.emaBps * 0.7 + instantBps * 0.3
          sample.lastAt = timestamp
          sample.lastLoaded = prog.loaded
        }
        const elapsed = (timestamp - sample.startedAt) / 1000
        if (elapsed >= 0.4 && prog.loaded >= 32 * 1024 && sample.emaBps && sample.emaBps > 0) {
          speedBps = sample.emaBps
          if (prog.lengthComputable && prog.total > prog.loaded) {
            etaSeconds = (prog.total - prog.loaded) / speedBps
          }
        }
      }

      const isComplete =
        Boolean(prog.uploadComplete) ||
        (prog.lengthComputable && prog.total > 0 && prog.loaded >= prog.total)

      setUploadState({
        filename: file.name,
        loaded: prog.loaded,
        total: prog.total,
        lengthComputable: prog.lengthComputable,
        phase: isComplete ? 'validating' : 'uploading',
        speedBps: isComplete ? null : speedBps,
        etaSeconds: isComplete ? null : etaSeconds,
      })
    }

    void (async () => {
      try {
        const source = await api.uploadSource(file, {
          onProgress: handleProgress,
          signal: controller.signal,
        })
        if (!mountedRef.current) return
        if (controller.signal.aborted) {
          setStatusNote('Upload cancelled')
          return
        }
        uploadAbortRef.current = null
        setUploadState((prev) =>
          prev
            ? { ...prev, phase: 'starting', speedBps: null, etaSeconds: null }
            : null,
        )
        const job = await api.createJob(source.id, jobOverrides)
        if (!mountedRef.current) return
        navigate(`/jobs/${job.id}`)
      } catch (err) {
        if (!mountedRef.current) return
        if (isAbortError(err) || controller.signal.aborted) {
          setStatusNote('Upload cancelled')
        } else {
          setError(err as Error)
        }
      } finally {
        if (uploadAbortRef.current === controller) {
          uploadAbortRef.current = null
        }
        speedSampleRef.current = null
        busyRef.current = false
        if (mountedRef.current) {
          setBusy(null)
          setUploadState(null)
          if (fileInput.current) {
            fileInput.current.value = ''
          }
        }
      }
    })()
  }

  const cancelUpload = () => {
    const controller = uploadAbortRef.current
    if (!controller) return
    controller.abort()
  }

  const submitExisting = (event: React.FormEvent) => {
    event.preventDefault()
    if (!selectedSourceId) return
    void start('existing', () => Promise.resolve({ id: selectedSourceId }))
  }

  const submitLibrary = (path: string) => {
    if (!path) return
    void start('library', () => api.importLibrary(path))
  }

  const usableProvider = providers.find((p) => p.available)
  const recentDoneJobForSelected = selectedSourceId
    ? jobs.find((j) => j.source_id === selectedSourceId && j.status === 'done')
    : undefined
  const isBusy = busy !== null
  const canCancelUpload =
    busy === 'file' &&
    uploadState !== null &&
    uploadState.phase === 'uploading' &&
    uploadAbortRef.current !== null
  const uploadPercent =
    uploadState && uploadState.lengthComputable && uploadState.total > 0
      ? Math.min(100, Math.round((uploadState.loaded / uploadState.total) * 100))
      : null

  return (
    <div className="pt-14">
      {/* Masthead. Left-aligned and asymmetric — the field is the subject, not
          a centred hero card. */}
      <div className="rise max-w-3xl">
        <p className="eyebrow">Local · No accounts · No watermarks</p>
        <h1 className="mt-5 font-display text-[clamp(2.75rem,7vw,5.5rem)] leading-[0.95] text-ink-100">
          Long video in.
          <br />
          <span className="italic text-sodium-500">Shorts</span> out.
        </h1>
      </div>

      <div className="mt-16 grid grid-cols-1 gap-x-16 gap-y-12 lg:grid-cols-[minmax(0,1.35fr)_minmax(0,1fr)]">
        {/* URL */}
        <section className="rise" style={{ animationDelay: '90ms' }}>
          <form onSubmit={submitUrl}>
            <label htmlFor="url" className="eyebrow">
              Paste a link
            </label>
            <div className="mt-3 flex items-end gap-4">
              <input
                id="url"
                className="field font-display text-xl md:text-2xl"
                placeholder="https://youtube.com/watch?v=…"
                value={url}
                onChange={(e) => setUrl(e.target.value)}
                autoComplete="off"
                spellCheck={false}
                disabled={isBusy}
              />
              <button
                type="submit"
                className="btn btn-primary shrink-0"
                disabled={isBusy || !url.trim()}
              >
                {busy === 'url' ? 'Fetching…' : 'Start'}
              </button>
            </div>
          </form>

          <p className="mt-3 text-xs leading-relaxed text-ink-500">
            Only download video you own or have the rights to process.
          </p>

          <div className="mt-8 border-t border-ink-850 pt-6">
            <form onSubmit={submitExisting}>
              <label htmlFor="existing-source" className="eyebrow">
                Existing source
              </label>
              {sourcesLoading ? (
                <p className="mt-3 text-xs text-ink-500">Loading saved sources…</p>
              ) : sourcesError ? (
                <div className="mt-3 flex items-center gap-3 text-xs text-signal-bad">
                  <span>Could not load saved sources ({sourcesError}).</span>
                  <button
                    type="button"
                    onClick={loadSources}
                    disabled={isBusy}
                    className="btn btn-quiet text-xs"
                  >
                    Retry
                  </button>
                </div>
              ) : sources.length === 0 ? (
                <p className="mt-3 text-xs text-ink-500">No saved sources yet.</p>
              ) : (
                <>
                  <div className="mt-3 flex flex-wrap items-end gap-4">
                    <select
                      id="existing-source"
                      value={selectedSourceId}
                      onChange={(e) => setSelectedSourceId(e.target.value)}
                      disabled={isBusy}
                      className="field min-w-0 flex-1 cursor-pointer text-sm"
                    >
                      {sources.map((source) => (
                        <option key={source.id} value={source.id} className="bg-ink-850">
                          {source.title || source.filename || 'Untitled'} (
                          {formatDuration(source.duration_s)})
                        </option>
                      ))}
                    </select>
                    <button
                      type="submit"
                      className="btn btn-primary shrink-0"
                      disabled={isBusy || !selectedSourceId}
                    >
                      {busy === 'existing' ? 'Starting…' : 'Process again'}
                    </button>
                  </div>
                  {recentDoneJobForSelected && (
                    <p className="mt-3 text-xs text-ink-400">
                      Recent completed analysis available:{' '}
                      <a
                        href={`/jobs/${recentDoneJobForSelected.id}/clips`}
                        className="text-sodium-500 underline underline-offset-4"
                      >
                        Open clips
                      </a>
                    </p>
                  )}
                </>
              )}
            </form>
          </div>

          <AdvancedOptions
            open={advancedOpen}
            onToggle={() => setAdvancedOpen((v) => !v)}
            overrides={overrides}
            onChange={setOverrides}
            providers={providers}
          />
        </section>

        {/* Upload */}
        <section className="rise" style={{ animationDelay: '160ms' }}>
          <p className="eyebrow">Or drop a file</p>
          <div
            onDragOver={(e) => {
              e.preventDefault()
              if (!isBusy && !busyRef.current) setDragging(true)
            }}
            onDragLeave={() => setDragging(false)}
            onDrop={(e) => {
              e.preventDefault()
              setDragging(false)
              if (isBusy || busyRef.current) return
              const file = e.dataTransfer.files[0]
              if (file) submitFile(file)
            }}
            className={[
              'mt-3 flex min-h-52 flex-col items-center justify-center gap-2 border border-dashed px-6 text-center transition-colors duration-200',
              isBusy ? 'cursor-not-allowed opacity-60' : 'cursor-pointer',
              dragging
                ? 'border-sodium-500 bg-sodium-700/10'
                : 'border-ink-700 hover:border-ink-600',
            ].join(' ')}
            onClick={() => {
              if (!isBusy && !busyRef.current) fileInput.current?.click()
            }}
            role="button"
            tabIndex={isBusy ? -1 : 0}
            aria-disabled={isBusy}
            onKeyDown={(e) => {
              if (isBusy || busyRef.current) return
              if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault()
                fileInput.current?.click()
              }
            }}
          >
            <span role="status" aria-live="polite" className="font-display text-2xl text-ink-200">
              {busy === 'file'
                ? uploadState?.phase === 'validating'
                  ? 'Validating media…'
                  : uploadState?.phase === 'starting'
                    ? 'Starting job…'
                    : 'Uploading…'
                : 'Drop video or audio'}
            </span>
            <span className="text-xs text-ink-500">mp4 · mov · mkv · webm · mp3 · wav · m4a</span>
          </div>

          {busy === 'file' && uploadState && (
            <div className="mt-4 border border-ink-800 bg-ink-850/40 p-4">
              <div className="flex items-baseline justify-between gap-4">
                <span className="truncate text-xs text-ink-200" title={uploadState.filename}>
                  {uploadState.filename}
                </span>
                <span className="numeric shrink-0 text-xs text-ink-400">
                  {uploadState.phase === 'validating'
                    ? 'Validating media…'
                    : uploadState.phase === 'starting'
                      ? 'Starting job…'
                      : uploadPercent !== null
                        ? `${uploadPercent}%`
                        : formatBytes(uploadState.loaded)}
                </span>
              </div>

              <div
                role="progressbar"
                aria-label="File upload progress"
                aria-valuemin={0}
                aria-valuemax={uploadPercent !== null ? 100 : undefined}
                aria-valuenow={uploadPercent !== null ? uploadPercent : undefined}
                aria-valuetext={
                  uploadState.phase === 'validating'
                    ? 'Validating media'
                    : uploadState.phase === 'starting'
                      ? 'Starting job'
                      : uploadPercent !== null
                        ? `${uploadPercent}% (${formatBytes(uploadState.loaded)} of ${formatBytes(uploadState.total)})`
                        : `${formatBytes(uploadState.loaded)} uploaded`
                }
                className="mt-2 h-1.5 w-full overflow-hidden bg-ink-800"
              >
                <div
                  className={`h-full bg-sodium-500 transition-[width] duration-150 ${uploadPercent === null && uploadState.phase === 'uploading' ? 'animate-pulse' : ''}`}
                  style={{
                    width:
                      uploadPercent !== null
                        ? `${uploadPercent}%`
                        : '100%',
                  }}
                />
              </div>

              <div className="mt-2 flex flex-wrap items-center justify-between gap-2 text-xs text-ink-500">
                <span className="numeric">
                  {uploadState.lengthComputable && uploadState.total > 0
                    ? `${formatBytes(uploadState.loaded)} / ${formatBytes(uploadState.total)}`
                    : `${formatBytes(uploadState.loaded)} uploaded`}
                  {uploadState.phase === 'uploading' && uploadState.speedBps
                    ? ` · ${formatBytes(Math.round(uploadState.speedBps))}/s`
                    : ''}
                  {uploadState.phase === 'uploading' &&
                  uploadState.etaSeconds !== null &&
                  uploadState.etaSeconds >= 1
                    ? ` · ${formatDuration(uploadState.etaSeconds)} left`
                    : ''}
                </span>

                {canCancelUpload && (
                  <button
                    type="button"
                    onClick={cancelUpload}
                    className="btn btn-quiet text-xs"
                  >
                    Cancel
                  </button>
                )}
              </div>
            </div>
          )}

          {statusNote && !busy && (
            <p role="status" className="mt-3 text-xs text-ink-400">
              {statusNote}
            </p>
          )}

          <input
            ref={fileInput}
            type="file"
            className="hidden"
            accept="video/*,audio/*"
            disabled={isBusy}
            onChange={(e) => {
              if (isBusy || busyRef.current) return
              const file = e.target.files?.[0]
              if (file) submitFile(file)
              e.target.value = ''
            }}
          />

          <LibraryPicker
            disabled={isBusy}
            busy={busy === 'library'}
            onSelect={submitLibrary}
          />
        </section>
      </div>

      {error && (
        <div className="mt-10 max-w-3xl">
          <ErrorNote error={error} onDismiss={() => setError(null)} />
        </div>
      )}

      {providers.length > 0 && !usableProvider && (
        <p className="mt-10 max-w-3xl border-l-2 border-sodium-600 pl-4 text-sm text-ink-300">
          No AI provider is reachable yet, so clip selection will fail. Add an API key or
          start Ollama in{' '}
          <a href="/settings" className="text-sodium-500 underline underline-offset-4">
            Settings
          </a>
          .
        </p>
      )}

      <RecentJobs jobs={jobs} />
    </div>
  )
}

function AdvancedOptions({
  open,
  onToggle,
  overrides,
  onChange,
  providers,
}: {
  open: boolean
  onToggle: () => void
  overrides: JobSettingsOverrides
  onChange: (next: JobSettingsOverrides) => void
  providers: ProviderStatus[]
}) {
  const set = <K extends keyof JobSettingsOverrides>(key: K, value: JobSettingsOverrides[K]) =>
    onChange({ ...overrides, [key]: value })

  return (
    <div className="mt-8">
      <button type="button" onClick={onToggle} className="btn btn-quiet -ml-1">
        <span
          className="inline-block transition-transform duration-300"
          style={{ transform: open ? 'rotate(90deg)' : 'none' }}
          aria-hidden
        >
          ›
        </span>
        {open ? 'Hide options' : 'Options'}
      </button>

      {/* grid-template-rows rather than height, so the reveal animates without
          touching layout properties. */}
      <div
        className="grid transition-[grid-template-rows] duration-400 ease-[cubic-bezier(0.16,1,0.3,1)]"
        style={{ gridTemplateRows: open ? '1fr' : '0fr' }}
      >
        <div className="overflow-hidden">
          <div className="grid gap-x-8 gap-y-5 pt-5 sm:grid-cols-2">
            <Selector
              label="Provider"
              value={overrides.provider ?? ''}
              onChange={(v) => set('provider', v || undefined)}
              options={[
                { value: '', label: 'Use default' },
                ...providers.map((p) => ({
                  value: p.name,
                  label: p.available ? p.name : `${p.name} — unavailable`,
                  disabled: !p.available,
                })),
              ]}
            />
            <Selector
              label="Whisper model"
              value={overrides.whisper_model ?? ''}
              onChange={(v) => set('whisper_model', v || undefined)}
              options={[
                { value: '', label: 'Use default' },
                { value: 'tiny', label: 'tiny — fastest, roughest' },
                { value: 'base', label: 'base' },
                { value: 'small', label: 'small — balanced' },
                { value: 'medium', label: 'medium' },
                { value: 'large-v3', label: 'large-v3 — slowest, best' },
              ]}
            />
            <NumberField
              label="Max clips"
              value={overrides.max_clips}
              placeholder="10"
              min={1}
              max={50}
              onChange={(v) => set('max_clips', v)}
            />
            <div className="grid grid-cols-2 gap-4">
              <NumberField
                label="Min length (s)"
                value={overrides.min_duration_s}
                placeholder="20"
                min={5}
                max={300}
                onChange={(v) => set('min_duration_s', v)}
              />
              <NumberField
                label="Max length (s)"
                value={overrides.max_duration_s}
                placeholder="90"
                min={5}
                max={300}
                onChange={(v) => set('max_duration_s', v)}
              />
            </div>
            <label className="flex items-center gap-3 text-sm text-ink-200 sm:col-span-2">
              <input
                type="checkbox"
                checked={overrides.diarization ?? false}
                onChange={(e) => set('diarization', e.target.checked || undefined)}
                className="size-4 accent-sodium-500"
              />
              Multiple speakers — label who is talking, and cut to them
            </label>
          </div>
        </div>
      </div>
    </div>
  )
}

function Selector({
  label,
  value,
  onChange,
  options,
}: {
  label: string
  value: string
  onChange: (value: string) => void
  options: { value: string; label: string; disabled?: boolean }[]
}) {
  return (
    <label className="block">
      <span className="eyebrow">{label}</span>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="field mt-1 cursor-pointer text-sm"
      >
        {options.map((option) => (
          <option
            key={option.value}
            value={option.value}
            disabled={option.disabled}
            className="bg-ink-850"
          >
            {option.label}
          </option>
        ))}
      </select>
    </label>
  )
}

function NumberField({
  label,
  value,
  placeholder,
  min,
  max,
  onChange,
}: {
  label: string
  value: number | undefined
  placeholder: string
  min: number
  max: number
  onChange: (value: number | undefined) => void
}) {
  return (
    <label className="block">
      <span className="eyebrow">{label}</span>
      <input
        type="number"
        className="field numeric mt-1 text-sm"
        placeholder={placeholder}
        value={value ?? ''}
        min={min}
        max={max}
        onChange={(e) => onChange(e.target.value ? Number(e.target.value) : undefined)}
      />
    </label>
  )
}

function RecentJobs({ jobs }: { jobs: Job[] }) {
  if (jobs.length === 0) return null

  return (
    <section className="rise mt-24" style={{ animationDelay: '240ms' }}>
      <div className="flex items-baseline justify-between border-b border-ink-800 pb-3">
        <h2 className="eyebrow">Recent</h2>
        <span className="numeric text-xs text-ink-600">{jobs.length}</span>
      </div>

      <ul>
        {jobs.map((job) => (
          <li key={job.id}>
            <a
              href={job.status === 'done' ? `/jobs/${job.id}/clips` : `/jobs/${job.id}`}
              className="group grid grid-cols-[1fr_auto] items-baseline gap-4 border-b border-ink-850 py-4 transition-colors duration-200 hover:bg-ink-850/40 sm:grid-cols-[1fr_7rem_6rem_5rem]"
            >
              <span className="truncate text-[0.9375rem] text-ink-200 group-hover:text-ink-100">
                {job.source?.title || 'Untitled'}
              </span>
              <span className="numeric hidden text-xs text-ink-500 sm:block">
                {job.source ? formatDuration(job.source.duration_s) : '—'}
              </span>
              <span className="hidden text-xs text-ink-500 sm:block">{job.provider}</span>
              <StatusTag job={job} />
            </a>
          </li>
        ))}
      </ul>
    </section>
  )
}

function StatusTag({ job }: { job: Job }) {
  const tone: Record<string, string> = {
    done: 'text-signal-good',
    failed: 'text-signal-bad',
    running: 'text-sodium-500',
    queued: 'text-ink-400',
    cancelled: 'text-ink-500',
  }
  const label =
    job.status === 'running' ? `${Math.round(job.progress * 100)}%` : job.status

  return (
    <span className={`numeric justify-self-end text-xs ${tone[job.status] ?? 'text-ink-400'}`}>
      {label}
    </span>
  )
}
