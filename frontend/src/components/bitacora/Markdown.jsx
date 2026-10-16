import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

// Links leave the app; the default urlTransform already drops javascript: URLs.
const COMPONENTS = {
  // eslint-disable-next-line no-unused-vars -- `node` must not reach the DOM
  a: ({ node, ...props }) => <a {...props} target="_blank" rel="noopener noreferrer" />,
  img: ({ alt }) => <span>{alt}</span>,
}

const GFM = [remarkGfm]

/**
 * Markdown from the family, importers or the AI, rendered safely: no
 * rehype-raw, so raw HTML in the text is shown escaped, never executed.
 * `gfm` turns on GitHub-flavoured markdown (pipe tables, e.g. the regimen
 * table of a medication list's `list_note`).
 */
export default function Markdown({ children, className = '', gfm = false }) {
  return (
    <div className={`md ${className}`}>
      <ReactMarkdown components={COMPONENTS} remarkPlugins={gfm ? GFM : undefined}>
        {children}
      </ReactMarkdown>
    </div>
  )
}
