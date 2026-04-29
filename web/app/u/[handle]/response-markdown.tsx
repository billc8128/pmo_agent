'use client';

// Markdown rendering for agent_response_full.
//
// The text comes from an AI assistant's transcript — by definition,
// untrusted to render as raw HTML. react-markdown is safe by default:
// it parses Markdown into AST and emits known-safe React elements.
// We do NOT enable rehype-raw, which would re-introduce HTML
// injection.
//
// Plugins:
//   - remark-gfm: tables, task lists, autolinks, strikethrough
//   - rehype-highlight: syntax highlighting for fenced code blocks
//
// Styling: minimal Tailwind classes via the `components` prop. We
// avoid @tailwindcss/typography (prose) for now to keep the bundle
// small and the visual identity ours.

import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import rehypeHighlight from 'rehype-highlight';
import 'highlight.js/styles/github-dark.css';

export function ResponseMarkdown({ source }: { source: string }) {
  return (
    <div className="markdown text-sm leading-relaxed text-zinc-800 dark:text-zinc-200">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeHighlight]}
        components={{
          // Block-level
          p: (props) => <p className="my-2" {...props} />,
          h1: (props) => <h1 className="mt-4 mb-2 text-base font-semibold" {...props} />,
          h2: (props) => <h2 className="mt-4 mb-2 text-base font-semibold" {...props} />,
          h3: (props) => <h3 className="mt-3 mb-1 text-sm font-semibold" {...props} />,
          h4: (props) => <h4 className="mt-3 mb-1 text-sm font-semibold" {...props} />,
          ul: (props) => <ul className="my-2 ml-5 list-disc space-y-1" {...props} />,
          ol: (props) => <ol className="my-2 ml-5 list-decimal space-y-1" {...props} />,
          li: (props) => <li className="leading-relaxed" {...props} />,
          blockquote: (props) => (
            <blockquote
              className="my-3 border-l-2 border-zinc-300 pl-3 text-zinc-600 dark:border-zinc-700 dark:text-zinc-400"
              {...props}
            />
          ),
          hr: () => <hr className="my-4 border-zinc-200 dark:border-zinc-800" />,

          // Inline
          a: (props) => (
            <a
              {...props}
              target="_blank"
              rel="noopener noreferrer"
              className="text-indigo-600 underline decoration-dotted underline-offset-2 hover:decoration-solid dark:text-indigo-400"
            />
          ),
          strong: (props) => <strong className="font-semibold" {...props} />,
          em: (props) => <em className="italic" {...props} />,

          // Code: react-markdown calls this for both inline and fenced.
          // Inline code has no `className` (rehype-highlight only adds
          // classes to fenced code).
          code: ({ className, children, ...rest }) => {
            const isBlock = typeof className === 'string' && className.includes('language-');
            if (isBlock) {
              return (
                <code className={className} {...rest}>
                  {children}
                </code>
              );
            }
            return (
              <code
                className="rounded bg-zinc-100 px-1 py-0.5 font-mono text-[0.85em] text-zinc-800 dark:bg-zinc-800 dark:text-zinc-200"
                {...rest}
              >
                {children}
              </code>
            );
          },
          pre: (props) => (
            <pre
              className="my-3 overflow-x-auto rounded-md bg-zinc-900 p-3 text-[0.85em] leading-relaxed dark:bg-zinc-950"
              {...props}
            />
          ),

          // GFM tables
          table: (props) => (
            <div className="my-3 overflow-x-auto">
              <table className="min-w-full border-collapse text-xs" {...props} />
            </div>
          ),
          th: (props) => (
            <th
              className="border-b border-zinc-300 px-2 py-1 text-left font-semibold dark:border-zinc-700"
              {...props}
            />
          ),
          td: (props) => (
            <td className="border-b border-zinc-100 px-2 py-1 dark:border-zinc-800" {...props} />
          ),
        }}
      >
        {source}
      </ReactMarkdown>
    </div>
  );
}
