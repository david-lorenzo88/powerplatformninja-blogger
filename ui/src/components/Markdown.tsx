import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

// Rendered Markdown for drafts and review reports. `prose-invert` gives us dark
// typography; images are constrained so a wide cover or figure can't overflow.
export function Markdown({ children }: { children: string }) {
  return (
    <div className="prose prose-invert prose-sm max-w-none prose-pre:bg-slate-950 prose-pre:border prose-pre:border-slate-800 prose-code:text-accent prose-a:text-accent prose-img:max-w-full prose-headings:text-slate-100">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{children}</ReactMarkdown>
    </div>
  )
}
