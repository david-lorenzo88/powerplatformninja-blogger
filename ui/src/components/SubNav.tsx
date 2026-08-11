import { useQuery } from '@tanstack/react-query'
import { NavLink } from 'react-router-dom'
import { getPending } from '../api/client'

// Second-level navigation, rendered by a section screen rather than by the shell.
//
// The bottom tab bar is the constraint that makes this exist. It is five `flex-1`
// columns, and the shell's own comment explains why: a thumb aims at a region,
// and a fifth of the screen width is the region it gets. Nine destinations would
// be 43px each. So related screens share one tab and separate here instead —
// Stream/Feeds/Groups under News, Ideas/Drafts/Sources under Blog.
//
// It lives in the screens and not the shell because a section with one child
// should show nothing at all, and only the screen knows that.

export interface SubNavItem {
  to: string
  label: string
  badge?: number
  end?: boolean
}

function itemClass({ isActive }: { isActive: boolean }): string {
  const base =
    'inline-flex min-h-11 shrink-0 items-center gap-1.5 rounded-lg px-3 text-sm font-medium ' +
    'transition-colors lg:min-h-0 lg:py-1.5'
  return isActive
    ? `${base} bg-accent/15 text-accent`
    : `${base} text-slate-400 hover:bg-slate-800/60 hover:text-slate-200 active:text-slate-200`
}

// The two section bars, as components rather than exported arrays: a screen file
// that exports a plain constant trips react/only-export-components, and putting
// them here keeps the tab-to-children mapping in one place next to the shell's
// NAV, which is where anyone changing the grouping will look.
export function NewsSubNav() {
  return (
    <SubNav
      items={[
        { to: '/articles', label: 'Stream' },
        { to: '/feeds', label: 'Feeds' },
        { to: '/feed-groups', label: 'Groups' },
      ]}
    />
  )
}

export function LettersSubNav() {
  return (
    <SubNav
      items={[
        { to: '/newsletters', label: 'Newsletters', end: true },
        { to: '/newsletters/issues', label: 'Issues' },
        { to: '/newsletters/recipients', label: 'Recipients' },
      ]}
    />
  )
}

export function BlogSubNav() {
  // The same 15-second poll the shell runs, so TanStack serves both from one
  // request rather than two.
  const { data } = useQuery({
    queryKey: ['pending'],
    queryFn: getPending,
    refetchInterval: 15_000,
  })
  return (
    <SubNav
      items={[
        { to: '/topic-ideas', label: 'Ideas' },
        { to: '/drafts', label: 'Drafts' },
        { to: '/source-reviews', label: 'Sources', badge: data?.source_reviews ?? 0 },
      ]}
    />
  )
}

export function SubNav({ items }: { items: SubNavItem[] }) {
  if (items.length < 2) return null
  return (
    // Scrolls rather than wraps: a second row of navigation costs more vertical
    // space than a phone can spare above the first row of content.
    <nav className="-mx-1 flex gap-1 overflow-x-auto px-1 pb-0.5 [scrollbar-width:none]">
      {items.map((item) => (
        <NavLink key={item.to} to={item.to} end={item.end} className={itemClass}>
          {item.label}
          {item.badge ? (
            <span className="rounded-full bg-amber-500/20 px-1.5 text-xs font-semibold text-amber-300">
              {item.badge}
            </span>
          ) : null}
        </NavLink>
      ))}
    </nav>
  )
}
