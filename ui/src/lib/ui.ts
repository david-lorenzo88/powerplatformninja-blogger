// One home for the class strings four screens were each retyping, and the only
// place that encodes the two rules touch imposes on a layout built for a mouse.
//
// 44px minimum height on anything pressable: a control that is comfortable under
// a cursor is not necessarily reachable with a thumb. Every interactive token
// starts at `min-h-11` and only tightens from `lg` up, so the desktop density
// this app was designed with survives untouched.
//
// 16px minimum font on inputs: iOS Safari zooms the viewport when a focused
// input renders below 16px, and there is no gesture that undoes it. Hence
// `text-base lg:text-sm` — that is a workaround, not a type-scale preference.
//
// `active:` is paired with every `hover:`. On touch a hover style sticks after a
// tap until you press somewhere else, so hover alone gives a control no press
// feedback and leaves the last-tapped one looking selected.

const TAP = 'min-h-11 lg:min-h-0'

// Full-width by default because that is what a phone wants; filter bars add
// `lg:w-56` to get the desktop width back.
export const field =
  `${TAP} w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-base ` +
  'text-slate-200 placeholder:text-slate-600 focus:border-accent focus:outline-none ' +
  'lg:py-1.5 lg:text-sm'

// The inline variant — a control that sits inside a row rather than owning one,
// such as the trust-tier select on a source-review candidate. Still 44px and
// still 16px on touch; it only shrinks on desktop.
export const fieldSm =
  `${TAP} rounded-lg border border-slate-700 bg-slate-950 px-2 py-1 text-base text-slate-200 ` +
  'focus:border-accent focus:outline-none lg:py-1 lg:text-xs'

export const label = 'mb-1 block text-xs font-medium text-slate-400'

const btn =
  `${TAP} inline-flex items-center justify-center gap-2 rounded-lg px-4 py-2 text-sm font-semibold ` +
  'transition-colors disabled:cursor-not-allowed disabled:opacity-50'

export const primaryBtn = `${btn} bg-accent text-slate-950 hover:bg-accent-strong active:bg-accent-strong`
export const ghostBtn =
  `${btn} border border-slate-700 text-slate-300 hover:border-slate-500 hover:text-slate-100 ` +
  'active:border-accent'
export const quietBtn = `${btn} font-normal text-slate-400 hover:text-slate-200 active:text-slate-200`

export const card = 'rounded-xl border border-slate-800 bg-slate-900/40'

// A whole card as a single tap target — the mobile stand-in for a `<tr onClick>`.
// Anything else pressable belongs outside it: a button nested in a button is
// invalid markup, and the `stopPropagation` trick it would need is unreliable
// under touch.
export const rowCard =
  `${card} block w-full p-4 text-left transition-colors hover:border-slate-600 active:border-accent/60`
