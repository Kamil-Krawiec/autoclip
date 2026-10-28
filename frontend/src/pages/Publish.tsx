import { useCallback, useEffect, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'

import {
  api,
  formatBytes,
  formatDuration,
  type PrivacyStatus,
  type Publication,
  type PublicationStatus,
  type PublishCandidate,
  type PublishStatus,
} from '../api'
import { ErrorNote } from '../components/ErrorNote'

const STATUS_LABELS: Record<PublicationStatus, string> = {
  draft: 'Draft',
  ready: 'Ready',
  uploading: 'Uploading',
  published: 'Published',
  failed: 'Failed',
}

function statusClass(status: PublicationStatus): string {
  if (status === 'published') return 'text-signal-good'
  if (status === 'failed') return 'text-signal-bad'
  if (status === 'uploading') return 'text-sodium-400'
  if (status === 'ready') return 'text-ink-100'
  return 'text-ink-400'
}

function formatDate(iso: string): string {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return iso
  return date.toLocaleString('pl-PL', { dateStyle: 'medium', timeStyle: 'short' })
}

/** YouTube counts a spaced tag as if it were quoted, plus the separating commas. */
function tagsLength(tags: string[]): number {
  if (tags.length === 0) return 0
  return tags.reduce((total, tag) => total + tag.length + (tag.includes(' ') ? 2 : 0), 0) + tags.length - 1
}

export function PublishListPage() {
  const navigate = useNavigate()
  const [status, setStatus] = useState<PublishStatus | null>(null)
  const [candidates, setCandidates] = useState<PublishCandidate[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<Error | null>(null)
  const [opening, setOpening] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const [nextStatus, nextCandidates] = await Promise.all([
        api.publishStatus(),
        api.publishCandidates(),
      ])
      setStatus(nextStatus)
      setCandidates(nextCandidates)
    } catch (err) {
      setError(err as Error)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const open = async (candidate: PublishCandidate) => {
    setOpening(candidate.export_id)
    setError(null)
    try {
      const publication = candidate.publication_id
        ? await api.getPublication(candidate.publication_id)
        : await api.createPublication(candidate.export_id)
      navigate(`/publish/${publication.id}`)
    } catch (err) {
      setError(err as Error)
    } finally {
      setOpening(null)
    }
  }

  if (loading) {
    return <p className="pt-24 text-sm text-ink-300">Loading the publish queue…</p>
  }

  const unpublished = candidates.filter((item) => item.publication_status !== 'published')
  const published = candidates.filter((item) => item.publication_status === 'published')

  return (
    <section className="pt-10">
      <div className="flex flex-wrap items-end justify-between gap-4 border-b border-ink-800 pb-5">
        <div>
          <h1 className="font-display text-[clamp(2rem,4vw,3rem)] leading-none text-ink-100">
            Publish
          </h1>
          <p className="mt-3 max-w-2xl text-sm leading-relaxed text-ink-400">
            Finished exports, ready for YouTube. Nothing leaves this machine until you press
            Publish.
          </p>
        </div>
        <button className="btn btn-ghost" onClick={() => void load()} disabled={Boolean(opening)}>
          Refresh
        </button>
      </div>

      {error && (
        <div className="mt-6 max-w-3xl">
          <ErrorNote error={error} onDismiss={() => setError(null)} />
        </div>
      )}

      {status && (!status.openai_image_key_present || !status.youtube_ready) && (
        <SetupBanner status={status} />
      )}

      {candidates.length === 0 ? (
        <div className="mt-16 max-w-xl">
          <p className="text-sm text-ink-300">No ready shorts yet. Export a clip from Review first.</p>
          <Link to="/" className="btn btn-ghost mt-5 inline-flex">
            Go to New
          </Link>
        </div>
      ) : (
        <>
          <h2 className="eyebrow mt-12">Ready to publish</h2>
          {unpublished.length === 0 ? (
            <p className="mt-4 text-sm text-ink-400">Everything here is already on YouTube.</p>
          ) : (
            <div className="mt-5 space-y-3">
              {unpublished.map((candidate) => (
                <CandidateRow
                  key={candidate.export_id}
                  candidate={candidate}
                  busy={opening === candidate.export_id}
                  onOpen={() => void open(candidate)}
                />
              ))}
            </div>
          )}

          {published.length > 0 && (
            <>
              <h2 className="eyebrow mt-14">Published</h2>
              <div className="mt-5 space-y-3">
                {published.map((candidate) => (
                  <CandidateRow
                    key={candidate.export_id}
                    candidate={candidate}
                    busy={opening === candidate.export_id}
                    onOpen={() => void open(candidate)}
                  />
                ))}
              </div>
            </>
          )}
        </>
      )}
    </section>
  )
}

function SetupBanner({ status }: { status: PublishStatus }) {
  return (
    <div className="mt-6 max-w-3xl break-words border-l-2 border-sodium-600 bg-ink-850/40 py-4 pl-4 text-sm text-ink-300">
      <p className="font-medium text-ink-100">Publishing is not fully configured.</p>
      <ul className="mt-2 space-y-1.5">
        {!status.openai_image_key_present && (
          <li>
            No OpenAI Images key.{' '}
            <Link to="/settings" className="text-sodium-400 hover:text-sodium-300">
              Add it in Settings → Keys → OpenAI Images
            </Link>
            . The separate key keeps a DeepSeek/OpenRouter key away from OpenAI Images.
          </li>
        )}
        {!status.youtube_ready && (
          <li>
            YouTube OAuth is missing. Save a Desktop OAuth client as{' '}
            <code className="break-all text-ink-200">
              $AUTOCLIP_HOME/secrets/youtube_client_secret.json
            </code>{' '}
            and run <code className="text-ink-200">autoclip publish auth</code>. The README&apos;s
            “Publish to YouTube” section walks through it.
          </li>
        )}
      </ul>
    </div>
  )
}

function CandidateRow({
  candidate,
  busy,
  onOpen,
}: {
  candidate: PublishCandidate
  busy: boolean
  onOpen: () => void
}) {
  const status = candidate.publication_status
  const label = status ? STATUS_LABELS[status] : 'Not started'
  return (
    <article className="flex flex-col gap-4 border border-ink-800 p-3 sm:flex-row sm:items-center">
      <div className="relative w-20 shrink-0 overflow-hidden bg-ink-850">
        <video
          src={candidate.download_url}
          muted
          playsInline
          preload="metadata"
          className="aspect-[9/16] w-20 object-cover"
        />
        <span className="numeric absolute bottom-1 left-1 rounded bg-ink-900/80 px-1 text-[10px] text-ink-200">
          {formatDuration(candidate.duration_s)}
        </span>
      </div>

      <div className="min-w-0 flex-1">
        <p className="truncate text-sm font-medium text-ink-100">{candidate.clip_title}</p>
        <p className="mt-0.5 truncate text-xs text-ink-500">{candidate.source_title}</p>
        <p className="numeric mt-1 text-xs text-ink-400">
          {candidate.ratio} · {formatBytes(candidate.size_bytes)} · exported{' '}
          {formatDate(candidate.export_created_at)}
        </p>
      </div>

      <div className="flex items-center gap-3 sm:flex-col sm:items-end">
        <span className={`text-xs ${status ? statusClass(status) : 'text-ink-500'}`}>{label}</span>
        {!candidate.available ? (
          <span className="text-xs text-signal-bad">Export file is missing</span>
        ) : (
          <button className="btn btn-ghost" onClick={onOpen} disabled={busy}>
            {busy ? 'Opening…' : candidate.publication_id ? 'Open' : 'Publish'}
          </button>
        )}
      </div>
    </article>
  )
}

export function PublishDetailPage() {
  const { publicationId } = useParams()
  const [publication, setPublication] = useState<Publication | null>(null)
  const [status, setStatus] = useState<PublishStatus | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<Error | null>(null)
  const [busy, setBusy] = useState<'metadata' | 'thumbnail' | 'save' | 'upload' | null>(null)

  const [title, setTitle] = useState('')
  const [description, setDescription] = useState('')
  const [tagsText, setTagsText] = useState('')
  const [prompt, setPrompt] = useState('')
  const [privacy, setPrivacy] = useState<PrivacyStatus>('private')

  const apply = useCallback((next: Publication) => {
    setPublication(next)
    setTitle(next.title)
    setDescription(next.description)
    setTagsText(next.tags.join(', '))
    setPrompt(next.thumbnail_prompt)
    setPrivacy(next.privacy_status)
  }, [])

  const load = useCallback(async () => {
    if (!publicationId) return
    setLoading(true)
    setError(null)
    try {
      const [nextPublication, nextStatus] = await Promise.all([
        api.getPublication(publicationId),
        api.publishStatus(),
      ])
      apply(nextPublication)
      setStatus(nextStatus)
    } catch (err) {
      setError(err as Error)
    } finally {
      setLoading(false)
    }
  }, [apply, publicationId])

  useEffect(() => {
    void load()
  }, [load])

  // Poll only while an upload runs. The task lives on the server, so leaving
  // the page is safe; reopening reads the same row.
  useEffect(() => {
    if (!publicationId || publication?.status !== 'uploading') return
    let cancelled = false
    const timer = window.setInterval(() => {
      void api
        .getPublication(publicationId)
        .then((next) => {
          if (!cancelled) setPublication(next)
        })
        .catch((err) => {
          if (!cancelled) setError(err as Error)
        })
    }, 2000)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [publication?.status, publicationId])

  if (loading) {
    return <p className="pt-24 text-sm text-ink-300">Loading publication…</p>
  }
  if (!publication) {
    return (
      <div className="max-w-3xl pt-16">
        <ErrorNote error={error ?? new Error('Publication not found.')} />
        <Link to="/publish" className="btn btn-ghost mt-5 inline-flex">
          Back to Publish
        </Link>
      </div>
    )
  }

  const locked = publication.status === 'uploading' || publication.status === 'published'
  const tags = tagsText
    .split(',')
    .map((tag) => tag.trim())
    .filter(Boolean)
  const descriptionBytes = new TextEncoder().encode(description).length

  const save = async () => {
    setBusy('save')
    setError(null)
    try {
      apply(
        await api.patchPublication(publication.id, {
          title,
          description,
          tags,
          thumbnail_prompt: prompt,
          privacy_status: privacy,
        }),
      )
    } catch (err) {
      setError(err as Error)
    } finally {
      setBusy(null)
    }
  }

  const generateMetadata = async () => {
    if (
      (title.trim() || description.trim()) &&
      !window.confirm('Replace the current title and description with a new proposal?')
    ) {
      return
    }
    setBusy('metadata')
    setError(null)
    try {
      apply(await api.generatePublicationMetadata(publication.id))
    } catch (err) {
      setError(err as Error)
    } finally {
      setBusy(null)
    }
  }

  const generateThumbnail = async () => {
    setBusy('thumbnail')
    setError(null)
    try {
      apply(await api.generatePublicationThumbnail(publication.id, prompt.trim() || undefined))
    } catch (err) {
      setError(err as Error)
    } finally {
      setBusy(null)
    }
  }

  const publish = async () => {
    if (privacy !== 'private') {
      const label = privacy === 'public' ? 'public — it goes live immediately' : 'unlisted'
      if (
        !window.confirm(
          `Publish as ${label}? Privacy is set at upload; you can change it later in YouTube Studio.`,
        )
      ) {
        return
      }
    }
    setBusy('upload')
    setError(null)
    try {
      apply(await api.uploadPublication(publication.id))
    } catch (err) {
      setError(err as Error)
    } finally {
      setBusy(null)
    }
  }

  return (
    <section className="pt-8">
      <Link to="/publish" className="eyebrow hover:text-sodium-400">
        ← Publish
      </Link>

      <div className="mt-4 flex flex-wrap items-end justify-between gap-3 border-b border-ink-800 pb-5">
        <div className="min-w-0">
          <h1 className="truncate font-display text-2xl text-ink-100">{publication.clip_title}</h1>
          <p className="mt-1 truncate text-sm text-ink-400">{publication.source_title}</p>
        </div>
        <div className="text-right">
          <span className={`text-xs ${statusClass(publication.status)}`}>
            {STATUS_LABELS[publication.status]}
          </span>
          {status && (
            <p className="numeric mt-1 text-[11px] text-ink-600">
              {status.metadata_model} · {status.image_model}
            </p>
          )}
        </div>
      </div>

      {error && (
        <div className="mt-5 max-w-3xl">
          <ErrorNote error={error} onDismiss={() => setError(null)} />
        </div>
      )}

      <div className="mt-8 grid gap-8 lg:grid-cols-[minmax(0,340px)_minmax(0,1fr)]">
        <div>
          <div className="overflow-hidden border border-ink-800 bg-black">
            <video
              src={`/api/exports/${publication.export_id}/download`}
              controls
              playsInline
              preload="metadata"
              className="aspect-[9/16] w-full"
            />
          </div>
          <dl className="mt-4 space-y-2 text-xs">
            <Row label="Duration" value={formatDuration(publication.duration_s)} />
            <Row label="Ratio" value={publication.ratio} />
            <Row
              label="Export"
              value={`${formatDate(publication.export_created_at)} · the original file`}
            />
            {publication.best_day && <Row label="Suggested day" value={publication.best_day} />}
          </dl>
          <p className="mt-4 text-xs leading-relaxed text-ink-500">
            Editing the clip afterwards does not change this publication. It uses the export that
            existed when you created it.
          </p>
        </div>

        <div className="space-y-8">
          <div className="space-y-4">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <h2 className="eyebrow">Details</h2>
              <button
                className="btn btn-ghost"
                onClick={() => void generateMetadata()}
                disabled={locked || busy !== null}
              >
                {busy === 'metadata' ? 'Generating…' : 'Generate title & description'}
              </button>
            </div>

            <label className="block">
              <span className="eyebrow">Title</span>
              <input
                className="field mt-1 text-sm"
                value={title}
                maxLength={100}
                disabled={locked}
                onChange={(event) => setTitle(event.target.value)}
              />
              <span className="numeric mt-1 block text-right text-[11px] text-ink-600">
                {title.length}/100
              </span>
            </label>

            <label className="block">
              <span className="eyebrow">Description</span>
              <textarea
                className="field mt-1 min-h-40 text-sm"
                value={description}
                disabled={locked}
                onChange={(event) => setDescription(event.target.value)}
              />
              <span className="numeric mt-1 block text-right text-[11px] text-ink-600">
                {descriptionBytes}/5000 bytes
              </span>
            </label>

            <label className="block">
              <span className="eyebrow">Tags</span>
              <input
                className="field mt-1 text-sm"
                value={tagsText}
                disabled={locked}
                placeholder="comma-separated"
                onChange={(event) => setTagsText(event.target.value)}
              />
              <span className="numeric mt-1 block text-right text-[11px] text-ink-600">
                {tagsLength(tags)}/500
              </span>
            </label>

            <div className="grid gap-4 sm:grid-cols-2">
              <label className="block">
                <span className="eyebrow">Privacy at upload</span>
                <select
                  className="field mt-1 cursor-pointer text-sm"
                  value={privacy}
                  disabled={locked}
                  onChange={(event) => setPrivacy(event.target.value as PrivacyStatus)}
                >
                  <option value="private" className="bg-ink-850">
                    Private (default)
                  </option>
                  <option value="unlisted" className="bg-ink-850">
                    Unlisted
                  </option>
                  <option value="public" className="bg-ink-850">
                    Public
                  </option>
                </select>
              </label>
              <div className="flex items-end justify-end gap-3">
                <button className="btn btn-primary" onClick={() => void save()} disabled={locked || busy !== null}>
                  {busy === 'save' ? 'Saving…' : 'Save details'}
                </button>
              </div>
            </div>
            <p className="text-xs leading-relaxed text-ink-500">
              Privacy is set at upload. Change it later in YouTube Studio; this page cannot.
            </p>
          </div>

          <div className="space-y-4 border-t border-ink-800 pt-6">
            <h2 className="eyebrow">Thumbnail</h2>
            <div className="flex flex-col gap-5 sm:flex-row sm:items-start">
              <div className="relative w-44 shrink-0 overflow-hidden border border-ink-800 bg-ink-850">
                {publication.thumbnail_url ? (
                  <>
                    {/* The guide is a visual aid; the prompt is the source of truth. */}
                    <img
                      src={publication.thumbnail_url}
                      alt="Publication thumbnail"
                      className="aspect-[9/16] w-full object-cover"
                    />
                    <div className="pointer-events-none absolute inset-[8%] border border-white/40" />
                  </>
                ) : (
                  <div className="flex aspect-[9/16] items-center justify-center px-3 text-center text-xs text-ink-600">
                    No thumbnail yet
                  </div>
                )}
              </div>
              <div className="min-w-0 flex-1 space-y-3">
                <label className="block">
                  <span className="eyebrow">Prompt</span>
                  <textarea
                    className="field mt-1 min-h-32 text-sm"
                    value={prompt}
                    disabled={locked || busy === 'thumbnail'}
                    placeholder="Describe the image. The subject and text stay ~8–10% away from every edge."
                    onChange={(event) => setPrompt(event.target.value)}
                  />
                </label>
                <button
                  className="btn btn-ghost"
                  onClick={() => void generateThumbnail()}
                  disabled={locked || busy !== null || !prompt.trim()}
                >
                  {busy === 'thumbnail'
                    ? 'Generating…'
                    : publication.thumbnail_url
                      ? 'Retry with new prompt'
                      : 'Generate thumbnail'}
                </button>
                <p className="text-xs leading-relaxed text-ink-500">
                  The guide marks the safe area. The previous image stays until a new one succeeds.
                  Each attempt is a paid OpenAI Images call.
                </p>
              </div>
            </div>
          </div>

          <div className="space-y-4 border-t border-ink-800 pt-6">
            <h2 className="eyebrow">Upload</h2>
            {publication.status === 'uploading' && (
              <p className="numeric animate-pulse text-sm text-sodium-400" role="status">
                Uploading… You can leave this page; reopening shows the result.
              </p>
            )}
            {publication.status === 'published' && publication.youtube_url && (
              <div className="border-l-2 border-signal-good pl-4">
                <p className="text-sm font-medium text-ink-100">Published to YouTube.</p>
                <a
                  className="mt-1 block text-sm text-sodium-400 hover:text-sodium-300"
                  href={publication.youtube_url}
                  target="_blank"
                  rel="noreferrer"
                >
                  {publication.youtube_url}
                </a>
                {publication.youtube_video_id && (
                  <a
                    className="mt-1 block text-xs text-ink-400 hover:text-ink-200"
                    href={`https://studio.youtube.com/video/${publication.youtube_video_id}/edit`}
                    target="_blank"
                    rel="noreferrer"
                  >
                    Open in YouTube Studio
                  </a>
                )}
                {publication.uploaded_at && (
                  <p className="numeric mt-2 text-xs text-ink-500">
                    Uploaded {formatDate(publication.uploaded_at)}
                  </p>
                )}
              </div>
            )}
            {publication.error && (
              <div className="border-l-2 border-signal-bad pl-4 text-sm text-ink-200">
                <p className="font-medium text-ink-100">Last attempt failed</p>
                <p className="mt-1 whitespace-pre-line text-ink-400">{publication.error}</p>
              </div>
            )}
            {publication.status !== 'published' && (
              <button
                className="btn btn-primary"
                onClick={() => void publish()}
                disabled={locked || busy !== null || !publication.ready_to_upload}
              >
                {busy === 'upload' ? 'Starting…' : 'Publish to YouTube'}
              </button>
            )}
            {!publication.ready_to_upload && publication.status !== 'published' && (
              <p className="text-xs text-ink-500">
                A title and a generated thumbnail are required before uploading. The description is
                optional.
              </p>
            )}
          </div>
        </div>
      </div>
    </section>
  )
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline justify-between gap-4 border-b border-ink-850 pb-2">
      <dt className="text-ink-500">{label}</dt>
      <dd className="numeric truncate text-right text-ink-300">{value}</dd>
    </div>
  )
}
