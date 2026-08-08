import { useEffect } from 'react'
import type { ReactNode } from 'react'

// A centred dialog on desktop, a bottom sheet on touch. Backdrop click and
// Escape close it.
//
// The three-part flex column is the point. The previous single fixed div had
// neither a maximum height nor a scroll container, so on a 390x844 phone the
// Start-run form simply ran off the bottom of the screen and its submit button
// could not be reached at all — the dialog rendered, and the run could not be
// started. `max-h-[90dvh]` plus a `min-h-0 flex-1 overflow-y-auto` body is what
// guarantees the last control in `children` is always reachable, whatever a
// caller puts in there.
export function Modal({
  title,
  onClose,
  children,
  width = 'max-w-lg',
}: {
  title: string
  onClose: () => void
  children: ReactNode
  width?: string
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    // Without this iOS scrolls the page behind the sheet while you drag inside it.
    const previous = document.documentElement.style.overflow
    document.documentElement.style.overflow = 'hidden'
    return () => {
      window.removeEventListener('keydown', onKey)
      document.documentElement.style.overflow = previous
    }
  }, [onClose])

  return (
    <div
      className="fixed inset-0 z-50 flex items-end justify-center bg-black/60 lg:items-start lg:p-6 lg:pt-24"
      onClick={onClose}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label={title}
        onClick={(e) => e.stopPropagation()}
        className={`flex max-h-[90dvh] w-full flex-col rounded-t-2xl border border-slate-700 bg-slate-900 shadow-2xl lg:max-h-[calc(100dvh-8rem)] lg:rounded-xl ${width}`}
      >
        <div className="flex shrink-0 items-center justify-between border-b border-slate-800 py-2 pl-5 pr-2">
          <h2 className="truncate text-sm font-semibold text-slate-100">{title}</h2>
          <button
            onClick={onClose}
            className="flex h-11 w-11 shrink-0 items-center justify-center rounded text-slate-500 hover:bg-slate-800 hover:text-slate-200 active:bg-slate-800 lg:h-8 lg:w-8"
            aria-label="Close"
          >
            ✕
          </button>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain p-5 pb-[max(1.25rem,env(safe-area-inset-bottom))]">
          {children}
        </div>
      </div>
    </div>
  )
}
